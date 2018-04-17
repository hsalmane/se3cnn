# pylint: disable=C,R,E1101
import torch
import h5py
import numpy as np
import numbers
import sys


class MRISegmentation(torch.utils.data.Dataset):
    """Read 3D medical image files in .nii format, and provide it to the
       user as 3D patches of the requested size. Setting
       randomize_patch_offsets=True will add random offsets to the
       patches to reduce the affect of patch boundaries."""

    def __init__(self, h5_filename, filter, patch_shape, patch_overlap,
                 randomize_patch_offsets=True,
                 pad_mode='constant',
                 pad_constant=0):
                 # log10_signal=False):

        if isinstance(patch_shape, numbers.Integral):
            patch_shape = np.repeat(patch_shape, 3)
        if isinstance(patch_overlap, numbers.Integral):
            patch_overlap = np.repeat(patch_overlap, 3)
        self.patch_shape = patch_shape
        self.patch_overlap = patch_overlap
        self.randomize_patch_offsets = randomize_patch_offsets
        self.pad_mode = pad_mode
        self.pad_constant = pad_constant
        # self.log10_signal = log10_signal

        self.data = []
        self.labels = []
        self.unpadded_data_shape = []
        self.padding_boundary = []
        self.patch_indices = []

        # Read H5 file
        print("Reading data...", end="")
        sys.stdout.flush()
        with h5py.File(h5_filename, 'r') as hf:
            for name in filter:
                data = hf[name][:]
                # Assumption: voxel value and pixel are stored in last dim
                signal_volume = data[:,:,:,0].squeeze()
                label_volume  = data[:,:,:,1].squeeze()
                self.data.append(signal_volume)
                self.labels.append(label_volume)
                self.unpadded_data_shape.append(self.data[-1].shape)
                self.padding_boundary.append(None)
            self.class_count = hf['class_counts'][:]
        print("done.")

        # This first call to initialize_patch_indices will calculate the
        # total padding around each image, and store it in self.padding_boundary
        self.initialize_patch_indices()

        # The total padding is fixed to a multiple of the patch size - we
        # can therefore add the full padding in the setup fase
        print("Applying padding...", end="")
        sys.stdout.flush()
        for i, image in enumerate(self.data):
            pad_width = self.padding_boundary[i]
            self.data[i] = np.pad(self.data[i], pad_width,
                                  mode=self.pad_mode,
                                  constant_values=self.pad_constant)
            self.labels[i] = np.pad(self.labels[i], pad_width,
                                    mode=self.pad_mode,
                                    constant_values=self.pad_constant).astype(np.int64)
        print("done.")
        # # optionally logarithmize zero shifted input signal
        # if self.log10_signal:
        #     print('logarithmize signal')
        #     signal_min = min([np.min(data_i) for data_i in self.data])
        #     for i in range(len(self.data)):
        #         self.data[i] = np.log10(self.data[i] + signal_min + 1) # add 1 to prevent -inf from the log


    def get_original(self, dataset_index):
        """Get full input image at specified index"""
        size = self.unpadded_data_shape[dataset_index]
        patch_index_start = self.padding_boundary[dataset_index][:,0]
        patch_index_end = patch_index_start + size
        patch_index = np.stack((patch_index_start, patch_index_end))
        return (self.data[dataset_index][patch_index[0, 0]:patch_index[1, 0],
                                         patch_index[0, 1]:patch_index[1, 1],
                                         patch_index[0, 2]:patch_index[1, 2]],
                self.labels[dataset_index][patch_index[0, 0]:patch_index[1, 0],
                                         patch_index[0, 1]:patch_index[1, 1],
                                         patch_index[0, 2]:patch_index[1, 2]])

    def initialize_patch_indices(self):
        """For each image, calculate the indices for each patch, possibly
           shifted by a random offset"""

        self.patch_indices = []
        for i, image in enumerate(self.data):
            patch_indices, overflow = self.calc_patch_indices(
                self.unpadded_data_shape[i],
                self.patch_shape,
                overlap=self.patch_overlap,
                randomize_offset=self.randomize_patch_offsets)
            patch_indices = np.append(np.full(shape=(patch_indices.shape[0],1),
                                              fill_value=i),
                                      patch_indices, axis=1)
            self.patch_indices += patch_indices.tolist()

            # Keep track of how much each image has been padded
            if self.padding_boundary[i] is None:
                pad_width = np.stack([overflow, overflow], axis=1)
                self.padding_boundary[i] = pad_width

    def __getitem__(self, index):
        """Retrieve a single patch"""

        # Which image to retrieve patch from
        dataset_index = self.patch_indices[index][0]

        # Extract image and label
        image = self.data[dataset_index]
        labels = self.labels[dataset_index]

        # Obtain patch indices into original image
        patch_index_start = np.array(self.patch_indices[index][1:],
                                     dtype=np.int16)
        patch_index_end = patch_index_start + self.patch_shape

        patch_index = np.stack((patch_index_start, patch_index_end))
        patch_valid = np.stack(patch_index).clip(
            min=0, max=self.unpadded_data_shape[dataset_index]) - patch_index[0]

        # Update patch indices to padded image
        patch_index_padded = patch_index + self.padding_boundary[dataset_index][:,0]

        # Lookup image and add channel dimension
        image_patch = np.expand_dims(
            image[patch_index_padded[0, 0]:patch_index_padded[1, 0],
                  patch_index_padded[0, 1]:patch_index_padded[1, 1],
                  patch_index_padded[0, 2]:patch_index_padded[1, 2]],
            axis=0).astype(np.float32)
        labels_patch = np.expand_dims(
            labels[patch_index_padded[0, 0]:patch_index_padded[1, 0],
                   patch_index_padded[0, 1]:patch_index_padded[1, 1],
                   patch_index_padded[0, 2]:patch_index_padded[1, 2]],
            axis=0)

        # print("image: ", image_patch)

        # Check that patch has the correct size
        assert np.all(image_patch[0].shape == self.patch_shape)
        assert np.all(labels_patch[0].shape == self.patch_shape)

        return image_patch, labels_patch, dataset_index, patch_index, patch_valid

    def __len__(self):
        return len(self.patch_indices)

    @staticmethod
    def calc_patch_indices(image_shape, patch_shape,
                           overlap=0,
                           randomize_offset=True,
                           minimum_overflow_fraction=0.25):
        """
        Given the image shape and the patch shape, calculate the placement
        of patches. If randomize_offset is on, it will randomize the placement,
        so that the patch boundaries affect different regions in each epoch.
        There is natural room for this randomization whenever the image size
        if not divisible by the patch size, in the sense that the overflow
        can be placed arbitrarily in the beginning on the end. If you want
        to ensure that some randomization will always occur, you can set
        minimum_overflow_fraction, which will ensure that an extra patch will
        be added to provide extra overflow if necessary.

        :param image_shape: Shape if input image
        :param patch_shape: Shape of patch
        :param overlap: Allow patches to overlap with this number of voxels
        :param randomize_offset: Whether patch placement should be normalized
        :param minimum_overflow_fraction: Specify to force overflow beyond image
               to be at least this fraction of the patch size
        :return:
        """
        if isinstance(overlap, numbers.Integral):
            overlap = np.repeat(overlap, len(image_shape))

        # Effective patch shape
        eff_patch_shape = (patch_shape - overlap)

        # Number of patches (rounding up)
        n_patches = np.ceil((image_shape - patch_shape) / eff_patch_shape + 1).astype(int)

        # Overflow of voxels beyond image
        overflow = eff_patch_shape * n_patches - image_shape + overlap

        if randomize_offset:

            # Add extra patch for dimensions where minimum is below fraction
            extra_patch = (overflow / patch_shape) < minimum_overflow_fraction
            while extra_patch.any():
                overflow += extra_patch*eff_patch_shape
                n_patches += extra_patch
                extra_patch = (overflow / patch_shape) < minimum_overflow_fraction

            # Select random start index so that overlap is spread randomly
            # on both sides. If overflow is larger than patch_shape
            max_start_offset = overflow
            start_index = -np.array([np.random.choice(offset + 1)
                                     for offset in max_start_offset])
            # max_start_offset = np.minimum(overflow, patch_shape-1)
            # min_start_offset = np.maximum(0, overflow-max_start_offset)
            # minmax_start_offset = list(zip(min_start_offset, max_start_offset))
            # start_index = -np.array([np.random.choice(np.arange(offset[0],
            #                                                     offset[1]+1))
            #                          for offset in minmax_start_offset])
        else:

            # Set start index to overflow is spread evenly on both sides
            start_index = -np.ceil(overflow/2).astype(int)

            # In the non-randomize setting, we still one to make sure that the
            # last patch sticks outside the image with at least overlap/2, i.e,
            # that overflow/2 > overlap/2 (since the overflow is distributed
            # evenly on both sides when randomize_offset=True
            extra_patch = (overflow < overlap)
            while extra_patch.any():
                overflow += extra_patch*eff_patch_shape
                n_patches += extra_patch
                extra_patch = (overflow < overlap)


        # stop_index = image_shape + start_index + overflow
        step_size = eff_patch_shape
        stop_index = start_index + step_size*n_patches

        return (np.mgrid[start_index[0]:stop_index[0]:step_size[0],
                         start_index[1]:stop_index[1]:step_size[1],
                         start_index[2]:stop_index[2]:step_size[2]].reshape(3, -1).T,
                overflow)






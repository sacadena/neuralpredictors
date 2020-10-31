import warnings
from collections import OrderedDict

import numpy as np
import torch
from torch import nn
from torch.nn import Parameter
from torch.nn import functional as F
from torch.nn.modules import ModuleDict

from .readouts import Readout, PointPyramid2d, Gaussian3d, PointPooled2d, FullGaussian2d, UltraSparse


class Gaussian2d(nn.Module):
    """
    Instantiates an object that can used to learn a point in the core feature space for each neuron,
    sampled from a Gaussian distribution with some mean and variance at train but set to mean at test time, that best predicts its response.

    The readout receives the shape of the core as 'in_shape', the number of units/neurons being predicted as 'outdims', 'bias' specifying whether
    or not bias term is to be used and 'init_range' range for initialising the mean and variance of the gaussian distribution from which we sample to
    uniform distribution, U(-init_range,init_range) and  uniform distribution, U(0.0, 3*init_range) respectively.
    The grid parameter contains the normalized locations (x, y coordinates in the core feature space) and is clipped to [-1.1] as it a
    requirement of the torch.grid_sample function. The feature parameter learns the best linear mapping between the feature
    map from a given location, sample from Gaussian at train time but set to mean at eval time, and the unit's response with or without an additional elu non-linearity.

    Args:
        in_shape (list): shape of the input feature map [channels, width, height]
        outdims (int): number of output units
        bias (bool): adds a bias term
        init_mu_range (float): initialises the the mean with Uniform([-init_range, init_range])
                            [expected: positive value <=1]
        init_sigma_range (float): initialises sigma with Uniform([0.0, init_sigma_range]).
                It is recommended however to use a fixed initialization, for faster convergence.
                For this, set fixed_sigma to True.
        batch_sample (bool): if True, samples a position for each image in the batch separately
                            [default: True as it decreases convergence time and performs just as well]
        align_corners (bool): Keyword agrument to gridsample for bilinear interpolation.
                It changed behavior in PyTorch 1.3. The default of align_corners = True is setting the
                behavior to pre PyTorch 1.3 functionality for comparability.
        fixed_sigma (bool). Recommended behavior: True. But set to false for backwards compatibility.
                If true, initialized the sigma not in a range, but with the exact value given for all neurons.
    """

    def __init__(
        self,
        in_shape,
        outdims,
        bias,
        init_mu_range=0.5,
        init_sigma_range=0.5,
        batch_sample=True,
        align_corners=True,
        fixed_sigma=False,
        **kwargs
    ):
        warnings.warn(
            "Gaussian2d is deprecated and will be removed in the future. Use `layers.readout.NonIsoGaussian2d` instead",
            DeprecationWarning,
        )
        super().__init__()
        if init_mu_range > 1.0 or init_mu_range <= 0.0 or init_sigma_range <= 0.0:
            raise ValueError("either init_mu_range doesn't belong to [0.0, 1.0] or init_sigma_range is non-positive")
        self.in_shape = in_shape
        c, w, h = in_shape
        self.outdims = outdims
        self.batch_sample = batch_sample
        self.grid_shape = (1, outdims, 1, 2)
        self.mu = Parameter(torch.Tensor(*self.grid_shape))  # mean location of gaussian for each neuron
        self.sigma = Parameter(torch.Tensor(*self.grid_shape))  # standard deviation for gaussian for each neuron
        self.features = Parameter(torch.Tensor(1, c, 1, outdims))  # feature weights for each channel of the core

        if bias:
            bias = Parameter(torch.Tensor(outdims))
            self.register_parameter("bias", bias)
        else:
            self.register_parameter("bias", None)

        self.init_mu_range = init_mu_range
        self.init_sigma_range = init_sigma_range
        self.align_corners = align_corners
        self.fixed_sigma = fixed_sigma
        self.initialize()

    def initialize(self):
        """
        Initializes the mean, and sigma of the Gaussian readout along with the features weights
        """
        self.mu.data.uniform_(-self.init_mu_range, self.init_mu_range)
        if self.fixed_sigma:
            self.sigma.data.uniform_(self.init_sigma_range, self.init_sigma_range)
        else:
            self.sigma.data.uniform_(0, self.init_sigma_range)
            warnings.warn(
                "sigma is sampled from uniform distribuiton, instead of a fixed value. Consider setting "
                "fixed_sigma to True"
            )
        self.features.data.fill_(1 / self.in_shape[0])

        if self.bias is not None:
            self.bias.data.fill_(0)

    def sample_grid(self, batch_size, sample=None):
        """
        Returns the grid locations from the core by sampling from a Gaussian distribution
        Args:
            batch_size (int): size of the batch
            sample (bool/None): sample determines whether we draw a sample from Gaussian distribution, N(mu,sigma), defined per neuron
                            or use the mean, mu, of the Gaussian distribution without sampling.
                           if sample is None (default), samples from the N(mu,sigma) during training phase and
                             fixes to the mean, mu, during evaluation phase.
                           if sample is True/False, overrides the model_state (i.e training or eval) and does as instructed
        """
        with torch.no_grad():
            self.mu.clamp_(min=-1, max=1)  # at eval time, only self.mu is used so it must belong to [-1,1]
            self.sigma.clamp_(min=0)  # sigma/variance is always a positive quantity

        grid_shape = (batch_size,) + self.grid_shape[1:]

        sample = self.training if sample is None else sample

        if sample:
            norm = self.mu.new(*grid_shape).normal_()
        else:
            norm = self.mu.new(*grid_shape).zero_()  # for consistency and CUDA capability

        return torch.clamp(
            norm * self.sigma + self.mu, min=-1, max=1
        )  # grid locations in feature space sampled randomly around the mean self.mu

    @property
    def grid(self):
        return self.sample_grid(batch_size=1, sample=False)

    def feature_l1(self, average=True):
        """
        Returns the l1 regularization term either the mean or the sum of all weights
        Args:
            average(bool): if True, use mean of weights for regularization

        """
        if average:
            return self.features.abs().mean()
        else:
            return self.features.abs().sum()

    def forward(self, x, sample=None, shift=None, out_idx=None):
        """
        Propagates the input forwards through the readout
        Args:
            x: input data
            sample (bool/None): sample determines whether we draw a sample from Gaussian distribution, N(mu,sigma), defined per neuron
                            or use the mean, mu, of the Gaussian distribution without sampling.
                           if sample is None (default), samples from the N(mu,sigma) during training phase and
                             fixes to the mean, mu, during evaluation phase.
                           if sample is True/False, overrides the model_state (i.e training or eval) and does as instructed
            shift (bool): shifts the location of the grid (from eye-tracking data)
            out_idx (bool): index of neurons to be predicted

        Returns:
            y: neuronal activity
        """
        N, c, w, h = x.size()
        c_in, w_in, h_in = self.in_shape
        if (c_in, w_in, h_in) != (c, w, h):
            raise ValueError("the specified feature map dimension is not the readout's expected input dimension")
        feat = self.features.view(1, c, self.outdims)
        bias = self.bias
        outdims = self.outdims

        if self.batch_sample:
            # sample the grid_locations separately per image per batch
            grid = self.sample_grid(batch_size=N, sample=sample)  # sample determines sampling from Gaussian
        else:
            # use one sampled grid_locations for all images in the batch
            grid = self.sample_grid(batch_size=1, sample=sample).expand(N, outdims, 1, 2)

        if out_idx is not None:
            if isinstance(out_idx, np.ndarray):
                if out_idx.dtype == bool:
                    out_idx = np.where(out_idx)[0]
            feat = feat[:, :, out_idx]
            grid = grid[:, out_idx]
            if bias is not None:
                bias = bias[out_idx]
            outdims = len(out_idx)

        if shift is not None:
            grid = grid + shift[:, None, None, :]

        y = F.grid_sample(x, grid, align_corners=self.align_corners)
        y = (y.squeeze(-1) * feat).sum(1).view(N, outdims)

        if self.bias is not None:
            y = y + bias
        return y

    def __repr__(self):
        c, w, h = self.in_shape
        r = self.__class__.__name__ + " (" + "{} x {} x {}".format(c, w, h) + " -> " + str(self.outdims) + ")"
        if self.bias is not None:
            r += " with bias"
        for ch in self.children():
            r += "  -> " + ch.__repr__() + "\n"
        return r


class MultiReadout(Readout, ModuleDict):
    _base_readout = None

    def __init__(self, in_shape, loaders, gamma_readout, clone_readout=False, **kwargs):
        if self._base_readout is None:
            raise ValueError("Attribute _base_readout must be set")

        super().__init__()

        self.in_shape = in_shape
        self.neurons = OrderedDict([(k, loader.dataset.n_neurons) for k, loader in loaders.items()])
        if "positive" in kwargs:
            self._positive = kwargs["positive"]

        self.gamma_readout = gamma_readout  # regularisation strength

        for i, (k, n_neurons) in enumerate(self.neurons.items()):
            if i == 0 or clone_readout is False:
                self.add_module(k, self._base_readout(in_shape=in_shape, outdims=n_neurons, **kwargs))
                original_readout = k
            elif i > 0 and clone_readout is True:
                self.add_module(k, ClonedReadout(self[original_readout], **kwargs))

    def initialize(self, mean_activity_dict):
        for k, mu in mean_activity_dict.items():
            self[k].initialize()
            if hasattr(self[k], "bias"):
                self[k].bias.data = mu.squeeze() - 1

    def regularizer(self, readout_key):
        return self[readout_key].feature_l1() * self.gamma_readout

    @property
    def positive(self):
        if hasattr(self, "_positive"):
            return self._positive
        else:
            return False

    @positive.setter
    def positive(self, value):
        self._positive = value
        for k in self:
            self[k].positive = value


class MultiplePointPyramid2d(MultiReadout):
    _base_readout = PointPyramid2d


class MultipleGaussian3d(MultiReadout):
    """
    Instantiates multiple instances of Gaussian3d Readouts
    usually used when dealing with different datasets or areas sharing the same core.
    Args:
        in_shape (list): shape of the input feature map [channels, width, height]
        loaders (list):  a list of dataset objects
        gamma_readout (float): regularisation term for the readout which is usally set to 0.0 for gaussian3d readout
                               as it contains one dimensional weight

    """

    _base_readout = Gaussian3d

    # Make sure this is not a bug
    def regularizer(self, readout_key):
        return self.gamma_readout


class MultiplePointPooled2d(MultiReadout):
    """
    Instantiates multiple instances of PointPool2d Readouts
    usually used when dealing with more than one dataset sharing the same core.
    """

    _base_readout = PointPooled2d


class MultipleFullGaussian2d(MultiReadout):
    """
    Instantiates multiple instances of FullGaussian2d Readouts
    usually used when dealing with more than one dataset sharing the same core.

    Args:
        in_shape (list): shape of the input feature map [channels, width, height]
        loaders (list):  a list of dataloaders
        gamma_readout (float): regularizer for the readout
    """

    _base_readout = FullGaussian2d


class MultipleUltraSparse(MultiReadout):
    """
    This class instantiates multiple instances of UltraSparseReadout
    useful when dealing with multiple datasets
    Args:
        in_shape (list): shape of the input feature map [channels, width, height]
        loaders (list):  a list of dataset objects
        gamma_readout (float): regularisation term for the readout which is usally set to 0.0 for UltraSparseReadout readout
                               as it contains one dimensional weight
    """

    _base_readout = UltraSparse


class ClonedReadout(Readout, nn.Module):
    """
    This readout clones another readout while applying a linear transformation on the output. Used for MultiDatasets
    with matched neurons where the x-y positions in the grid stay the same but the predicted responses are rescaled due
    to varying experimental conditions.
    """

    def __init__(self, original_readout, **kwargs):
        super().__init__()

        self._source = original_readout
        self.alpha = Parameter(torch.ones(self._source.features.shape[-1]))
        self.beta = Parameter(torch.zeros(self._source.features.shape[-1]))

    def forward(self, x):
        x = self._source(x) * self.alpha + self.beta
        return x

    def feature_l1(self, average=True):
        """ Regularization is only applied on the scaled feature weights, not on the bias."""
        if average:
            return (self._source.features * self.alpha).abs().mean()
        else:
            return (self._source.features * self.alpha).abs().sum()

    def initialize(self):
        self.alpha.data.fill_(1.0)
        self.beta.data.fill_(0.0)

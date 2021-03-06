"""Modified from https://github.com/CSAILVision/semantic-segmentation-pytorch"""

import os

import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
from scipy.io import loadmat
from torch.nn.modules import BatchNorm2d

from . import resnet

# constants
ARCH_ENCODER = "resnet50dilated"
ARCH_DECODER = "ppm_deepsup"
FC_DIM = 2048
NUM_CLASS = 150
CLASS_SKY = 2
EXTENDED_CLASSES = [
    26,  # Sea
    113,  # waterfall;falls
]
DYNAMIC_CLASSES = {"sky": 2,
                   "water": 21,
                   "sea": 26,
                   "river": 60,
                   "fountain": 104,
                   "swimming": 109,
                   "waterfall": 113,}

STATIC_CLASSES = {"wall": 0,
                  "building": 1,
                  "tree": 4,
                  "road": 6,
                  "grass": 9,
                  "sidewalk": 11,
                  "person" : 12,
                  "earth": 13,
                  "mountain": 16,
                  "car": 20,
                  "house": 25,
                  "field": 29,
                  "rock": 34,
                  "railing": 38,
                  "column": 42,
                  "sand": 46,
                  "skyscraper": 48,
                  "stairs": 53,
                  "pool": 56,
                  "bridge": 61,
                  "flower": 66,
                  "hill": 68,
                  "bench": 69,
                  "boat": 76,
                  "truck": 83,
                  "tower": 84,
                  "streetlight": 87,
                  "land": 94,
                  "ship": 103,
                  "animal": 126,
                  "bicycle": 127}

GRASS_CLASSES = {
    "tree": 4,
    "grass": 9,
    "field": 29,
}


DYNAMIC_CLASSES_LIST = list(DYNAMIC_CLASSES.values())
STATIC_CLASSES_LIST = list(STATIC_CLASSES.values())
GRASS_CLASSES_LIST = list(GRASS_CLASSES.values())


# useful things
base_path = os.path.dirname(os.path.abspath(__file__))  # current file path
colors_path = os.path.join(base_path, 'color150.mat')
classes_path = os.path.join(base_path, 'object150_info.csv')
weights_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))), "weights")

segm_options = dict(colors=loadmat(colors_path)['colors'],
                    classes=pd.read_csv(classes_path),)


class NormalizeTensor:
    def __init__(self, mean, std, inplace=False):
        """Normalize a tensor image with mean and standard deviation.
        .. note::
            This transform acts out of place by default, i.e., it does not mutates the input tensor.
        See :class:`~torchvision.transforms.Normalize` for more details.
        Args:
            tensor (Tensor): Tensor image of size (C, H, W) to be normalized.
            mean (sequence): Sequence of means for each channel.
            std (sequence): Sequence of standard deviations for each channel.
            inplace(bool,optional): Bool to make this operation inplace.
        Returns:
            Tensor: Normalized Tensor image.
        """

        self.mean = mean
        self.std = std
        self.inplace = inplace

    def __call__(self, tensor):
        if not self.inplace:
            tensor = tensor.clone()

        dtype = tensor.dtype
        mean = torch.as_tensor(self.mean, dtype=dtype, device=tensor.device)
        std = torch.as_tensor(self.std, dtype=dtype, device=tensor.device)
        tensor.sub_(mean[None, :, None, None]).div_(std[None, :, None, None])
        return tensor


# Model Builder
class ModelBuilder:
    # custom weights initialization
    @staticmethod
    def weights_init(m):
        classname = m.__class__.__name__
        if classname.find('Conv') != -1:
            nn.init.kaiming_normal_(m.weight.data)
        elif classname.find('BatchNorm') != -1:
            m.weight.data.fill_(1.)
            m.bias.data.fill_(1e-4)

    @staticmethod
    def build_encoder(arch='resnet50dilated', fc_dim=512, weights=''):
        pretrained = True if len(weights) == 0 else False
        arch = arch.lower()

        if arch == 'resnet50dilated':
            orig_resnet = resnet.__dict__['resnet50'](pretrained=pretrained)
            net_encoder = ResnetDilated(orig_resnet, dilate_scale=8)
        else:
            raise Exception('Architecture undefined!')

        # encoders are usually pretrained
        # net_encoder.apply(ModelBuilder.weights_init)
        if len(weights) > 0:
            print('Loading weights for net_encoder')
            net_encoder.load_state_dict(
                torch.load(weights, map_location=lambda storage, loc: storage), strict=False)
        return net_encoder

    @staticmethod
    def build_decoder(arch='ppm_deepsup',
                      fc_dim=512, num_class=NUM_CLASS,
                      weights='', use_softmax=False):
        arch = arch.lower()
        if arch == 'ppm_deepsup':
            net_decoder = PPMDeepsup(
                num_class=num_class,
                fc_dim=fc_dim,
                use_softmax=use_softmax)
        else:
            raise Exception('Architecture undefined!')

        net_decoder.apply(ModelBuilder.weights_init)
        if len(weights) > 0:
            print('Loading weights for net_decoder')
            net_decoder.load_state_dict(
                torch.load(weights, map_location=lambda storage, loc: storage), strict=False)
        return net_decoder

    @staticmethod
    def get_decoder():
        path = os.path.join(weights_path, 'ade20k-resnet50dilated-ppm_deepsup/decoder_epoch_20.pth')
        return ModelBuilder.build_decoder(arch=ARCH_DECODER, fc_dim=FC_DIM, weights=path, use_softmax=True)

    @staticmethod
    def get_encoder():
        path = os.path.join(weights_path, 'ade20k-resnet50dilated-ppm_deepsup/encoder_epoch_20.pth')
        return ModelBuilder.build_encoder(arch=ARCH_ENCODER, fc_dim=FC_DIM, weights=path)


def conv3x3_bn_relu(in_planes, out_planes, stride=1):
    return nn.Sequential(
        nn.Conv2d(in_planes, out_planes, kernel_size=3, stride=stride, padding=1, bias=False),
        BatchNorm2d(out_planes),
        nn.ReLU(inplace=True),
    )


class SegmentationModule(nn.Module):
    def __init__(self,
                 net_enc=None,
                 net_dec=None,
                 encode='stationary_not_dynamic_probs',
                 use_default_normalization=False,
                 return_feature_maps=False,
                 return_feature_maps_level=3,  # {0, 1, 2, 3}
                 return_feature_maps_only=True,
                 unsqueeze_predictions=True,
                 **kwargs,
                 ):
        super().__init__()
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.encoder = ModelBuilder.get_encoder() if net_enc is None else net_enc
        self.decoder = ModelBuilder.get_decoder() if net_dec is None else net_dec
        self.use_default_normalization = use_default_normalization
        self.default_normalization = NormalizeTensor(mean=[0.485, 0.456, 0.406],
                                                     std=[0.229, 0.224, 0.225])

        self.encode = encode

        self.return_feature_maps = return_feature_maps

        assert 0 <= return_feature_maps_level <= 3
        self.return_feature_maps_level = return_feature_maps_level
        self.return_feature_maps_only = return_feature_maps_only
        self.unsqueeze_predictions = unsqueeze_predictions

    def normalize_input(self, tensor):
        if tensor.min() < 0 or tensor.max() > 1:
            raise ValueError("Tensor should be 0..1 before using normalize_input")
        return self.default_normalization(tensor)

    @property
    def feature_maps_channels(self):
        return 256 * 2**(self.return_feature_maps_level)  # 256, 512, 1024, 2048

    def forward(self, img_data, segSize=None):
        if segSize is None:
            raise NotImplementedError("Please pass segSize param. By default: (300, 300)")

        fmaps = self.encoder(img_data, return_feature_maps=True)
        pred = self.decoder(fmaps, segSize=segSize)

        if self.return_feature_maps:
            return pred, fmaps
        # print("BINARY", img_data.shape, pred.shape)
        return pred

    def multi_mask_from_multiclass(self, pred, classes):
        def isin(ar1, ar2):
            return (ar1[..., None] == ar2).any(-1).float()
        return isin(pred, torch.LongTensor(classes).to(self.device))

    @staticmethod
    def multi_mask_from_multiclass_probs(scores, classes):
        res = None
        for c in classes:
            if res is None:
                res = scores[:, c]
            else:
                res += scores[:, c]
        return res

    def predict(self, tensor, imgSizes=(256,),  # (300, 375, 450, 525, 600)
                segSize=None):
        """Entry-point for segmentation. Use this methods instead of forward
        Arguments:
            tensor {torch.Tensor} -- BCHW
        Keyword Arguments:
            imgSizes {tuple or list} -- imgSizes for segmentation input.
                default: (300, 450)
                original implementation: (300, 375, 450, 525, 600)

        """
        if segSize is None:
            segSize = tensor.shape[-2:] 
        segSize = (tensor.shape[2], tensor.shape[3])
        with torch.no_grad():
            if self.use_default_normalization:
                tensor = self.normalize_input(tensor)
            scores = torch.zeros(1, NUM_CLASS, segSize[0], segSize[1]).to(self.device)
            features = torch.zeros(1, self.feature_maps_channels, segSize[0], segSize[1]).to(self.device)

            result = []
            for img_size in imgSizes:
                if img_size != -1:
                    img_data = F.interpolate(tensor.clone(), size=img_size)
                else:
                    img_data = tensor.clone()

                if self.return_feature_maps:
                    raise NotImplementedError("For muti hannel mask not implemented")
                    pred_current, fmaps = self.forward(img_data, segSize=segSize)
                else:
                    pred_current = self.forward(img_data, segSize=segSize)


                result.append(pred_current)
                # scores = scores + pred_current / len(imgSizes)

                # Disclaimer: We use and aggregate only last fmaps: fmaps[3]
                if self.return_feature_maps:
                    features = features + F.interpolate(fmaps[self.return_feature_maps_level], size=segSize) / len(imgSizes)

            if self.encode == 'stationary_probs':
                final_res = []
                for scores in result:
                    tmp = self.multi_mask_from_multiclass_probs(scores, DYNAMIC_CLASSES_LIST)
                    final_res.append(tmp.unsqueeze(1))
                final_res = torch.cat(final_res, dim=1)
            elif self.encode == 'stationary_not_dynamic_probs':
                final_res = []
                for l, d in zip([DYNAMIC_CLASSES_LIST, STATIC_CLASSES_LIST, GRASS_CLASSES_LIST], [0, 1, 2]):
                    for scores in result:
                        tmp = self.multi_mask_from_multiclass_probs(scores, l)
                        if d in [1, 2]:
                            tmp = 1 - tmp
                        final_res.append(tmp.unsqueeze(1))
                final_res = torch.cat(final_res, dim=1)
            else:
                raise NotImplementedError()

            return final_res

    def get_edges(self, t):
        edge = torch.cuda.ByteTensor(t.size()).zero_()
        edge[:, :, :, 1:] = edge[:, :, :, 1:] | (t[:, :, :, 1:] != t[:, :, :, :-1])
        edge[:, :, :, :-1] = edge[:, :, :, :-1] | (t[:, :, :, 1:] != t[:, :, :, :-1])
        edge[:, :, 1:, :] = edge[:, :, 1:, :] | (t[:, :, 1:, :] != t[:, :, :-1, :])
        edge[:, :, :-1, :] = edge[:, :, :-1, :] | (t[:, :, 1:, :] != t[:, :, :-1, :])

        if True:
            return edge.half()
        return edge.float()


# pyramid pooling, deep supervision
class PPMDeepsup(nn.Module):
    def __init__(self, num_class=NUM_CLASS, fc_dim=4096,
                 use_softmax=False, pool_scales=(1, 2, 3, 6)):
        super().__init__()
        self.use_softmax = use_softmax

        self.ppm = []
        for scale in pool_scales:
            self.ppm.append(nn.Sequential(
                nn.AdaptiveAvgPool2d(scale),
                nn.Conv2d(fc_dim, 512, kernel_size=1, bias=False),
                BatchNorm2d(512),
                nn.ReLU(inplace=True)
            ))
        self.ppm = nn.ModuleList(self.ppm)
        self.cbr_deepsup = conv3x3_bn_relu(fc_dim // 2, fc_dim // 4, 1)

        self.conv_last = nn.Sequential(
            nn.Conv2d(fc_dim + len(pool_scales) * 512, 512,
                      kernel_size=3, padding=1, bias=False),
            BatchNorm2d(512),
            nn.ReLU(inplace=True),
            nn.Dropout2d(0.1),
            nn.Conv2d(512, num_class, kernel_size=1)
        )
        self.conv_last_deepsup = nn.Conv2d(fc_dim // 4, num_class, 1, 1, 0)
        self.dropout_deepsup = nn.Dropout2d(0.1)

    def forward(self, conv_out, segSize=None):
        conv5 = conv_out[-1]

        input_size = conv5.size()
        ppm_out = [conv5]
        for pool_scale in self.ppm:
            ppm_out.append(nn.functional.interpolate(
                pool_scale(conv5),
                (input_size[2], input_size[3]),
                mode='bilinear', align_corners=False))
        ppm_out = torch.cat(ppm_out, 1)

        x = self.conv_last(ppm_out)

        if self.use_softmax:  # is True during inference
            x = nn.functional.interpolate(
                x, size=segSize, mode='bilinear', align_corners=False)
            x = nn.functional.softmax(x, dim=1)
            return x

        # deep sup
        conv4 = conv_out[-2]
        _ = self.cbr_deepsup(conv4)
        _ = self.dropout_deepsup(_)
        _ = self.conv_last_deepsup(_)

        x = nn.functional.log_softmax(x, dim=1)
        _ = nn.functional.log_softmax(_, dim=1)

        return (x, _)


# Resnet Dilated
class ResnetDilated(nn.Module):
    def __init__(self, orig_resnet, dilate_scale=8):
        super().__init__()
        from functools import partial

        if dilate_scale == 8:
            orig_resnet.layer3.apply(
                partial(self._nostride_dilate, dilate=2))
            orig_resnet.layer4.apply(
                partial(self._nostride_dilate, dilate=4))
        elif dilate_scale == 16:
            orig_resnet.layer4.apply(
                partial(self._nostride_dilate, dilate=2))

        # take pretrained resnet, except AvgPool and FC
        self.conv1 = orig_resnet.conv1
        self.bn1 = orig_resnet.bn1
        self.relu1 = orig_resnet.relu1
        self.conv2 = orig_resnet.conv2
        self.bn2 = orig_resnet.bn2
        self.relu2 = orig_resnet.relu2
        self.conv3 = orig_resnet.conv3
        self.bn3 = orig_resnet.bn3
        self.relu3 = orig_resnet.relu3
        self.maxpool = orig_resnet.maxpool
        self.layer1 = orig_resnet.layer1
        self.layer2 = orig_resnet.layer2
        self.layer3 = orig_resnet.layer3
        self.layer4 = orig_resnet.layer4

    def _nostride_dilate(self, m, dilate):
        classname = m.__class__.__name__
        if classname.find('Conv') != -1:
            # the convolution with stride
            if m.stride == (2, 2):
                m.stride = (1, 1)
                if m.kernel_size == (3, 3):
                    m.dilation = (dilate // 2, dilate // 2)
                    m.padding = (dilate // 2, dilate // 2)
            # other convoluions
            else:
                if m.kernel_size == (3, 3):
                    m.dilation = (dilate, dilate)
                    m.padding = (dilate, dilate)

    def forward(self, x, return_feature_maps=False):
        conv_out = []

        x = self.relu1(self.bn1(self.conv1(x)))
        x = self.relu2(self.bn2(self.conv2(x)))
        x = self.relu3(self.bn3(self.conv3(x)))
        x = self.maxpool(x)

        x = self.layer1(x)
        conv_out.append(x)
        x = self.layer2(x)
        conv_out.append(x)
        x = self.layer3(x)
        conv_out.append(x)
        x = self.layer4(x)
        conv_out.append(x)

        if return_feature_maps:
            return conv_out
        return [x]
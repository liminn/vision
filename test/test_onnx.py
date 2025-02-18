import io
import torch
from torchvision import ops
from torchvision import models
from torchvision.models.detection.image_list import ImageList
from torchvision.models.detection.transform import GeneralizedRCNNTransform
from torchvision.models.detection.rpn import AnchorGenerator, RPNHead, RegionProposalNetwork
from torchvision.models.detection.backbone_utils import resnet_fpn_backbone
from torchvision.models.detection.roi_heads import RoIHeads
from torchvision.models.detection.faster_rcnn import FastRCNNPredictor, TwoMLPHead

from collections import OrderedDict

# onnxruntime requires python 3.5 or above
try:
    import onnxruntime
except ImportError:
    onnxruntime = None

import unittest


@unittest.skipIf(onnxruntime is None, 'ONNX Runtime unavailable')
class ONNXExporterTester(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        torch.manual_seed(123)

    def run_model(self, model, inputs_list, tolerate_small_mismatch=False):
        model.eval()

        onnx_io = io.BytesIO()
        # export to onnx with the first input
        torch.onnx.export(model, inputs_list[0], onnx_io, do_constant_folding=True, opset_version=10)

        # validate the exported model with onnx runtime
        for test_inputs in inputs_list:
            with torch.no_grad():
                if isinstance(test_inputs, torch.Tensor) or \
                   isinstance(test_inputs, list):
                    test_inputs = (test_inputs,)
                test_ouputs = model(*test_inputs)
                if isinstance(test_ouputs, torch.Tensor):
                    test_ouputs = (test_ouputs,)
            self.ort_validate(onnx_io, test_inputs, test_ouputs, tolerate_small_mismatch)

    def ort_validate(self, onnx_io, inputs, outputs, tolerate_small_mismatch=False):

        inputs, _ = torch.jit._flatten(inputs)
        outputs, _ = torch.jit._flatten(outputs)

        def to_numpy(tensor):
            if tensor.requires_grad:
                return tensor.detach().cpu().numpy()
            else:
                return tensor.cpu().numpy()

        inputs = list(map(to_numpy, inputs))
        outputs = list(map(to_numpy, outputs))

        ort_session = onnxruntime.InferenceSession(onnx_io.getvalue())
        # compute onnxruntime output prediction
        ort_inputs = dict((ort_session.get_inputs()[i].name, inpt) for i, inpt in enumerate(inputs))
        ort_outs = ort_session.run(None, ort_inputs)
        for i in range(0, len(outputs)):
            try:
                torch.testing.assert_allclose(outputs[i], ort_outs[i], rtol=1e-03, atol=1e-05)
            except AssertionError as error:
                if tolerate_small_mismatch:
                    assert ("(0.00%)" in str(error)), str(error)
                else:
                    assert False, str(error)

    def test_nms(self):
        boxes = torch.rand(5, 4)
        boxes[:, 2:] += torch.rand(5, 2)
        scores = torch.randn(5)

        class Module(torch.nn.Module):
            def forward(self, boxes, scores):
                return ops.nms(boxes, scores, 0.5)

        self.run_model(Module(), [(boxes, scores)])

    def test_roi_align(self):
        x = torch.rand(1, 1, 10, 10, dtype=torch.float32)
        single_roi = torch.tensor([[0, 0, 0, 4, 4]], dtype=torch.float32)
        model = ops.RoIAlign((5, 5), 1, 2)
        self.run_model(model, [(x, single_roi)])

    def test_roi_pool(self):
        x = torch.rand(1, 1, 10, 10, dtype=torch.float32)
        rois = torch.tensor([[0, 0, 0, 4, 4]], dtype=torch.float32)
        pool_h = 5
        pool_w = 5
        model = ops.RoIPool((pool_h, pool_w), 2)
        self.run_model(model, [(x, rois)])

    @unittest.skip("Disable test until Resize opset 11 is implemented in ONNX Runtime")
    def test_transform_images(self):

        class TransformModule(torch.nn.Module):
            def __init__(self_module):
                super(TransformModule, self_module).__init__()
                self_module.transform = self._init_test_generalized_rcnn_transform()

            def forward(self_module, images):
                return self_module.transform(images)[0].tensors

        input = [torch.rand(3, 800, 1280), torch.rand(3, 800, 800)]
        input_test = [torch.rand(3, 800, 1280), torch.rand(3, 800, 800)]
        self.run_model(TransformModule(), [input, input_test])

    def _init_test_generalized_rcnn_transform(self):
        min_size = 800
        max_size = 1333
        image_mean = [0.485, 0.456, 0.406]
        image_std = [0.229, 0.224, 0.225]
        transform = GeneralizedRCNNTransform(min_size, max_size, image_mean, image_std)
        return transform

    def _init_test_rpn(self):
        anchor_sizes = ((32,), (64,), (128,), (256,), (512,))
        aspect_ratios = ((0.5, 1.0, 2.0),) * len(anchor_sizes)
        rpn_anchor_generator = AnchorGenerator(anchor_sizes, aspect_ratios)
        out_channels = 256
        rpn_head = RPNHead(out_channels, rpn_anchor_generator.num_anchors_per_location()[0])
        rpn_fg_iou_thresh = 0.7
        rpn_bg_iou_thresh = 0.3
        rpn_batch_size_per_image = 256
        rpn_positive_fraction = 0.5
        rpn_pre_nms_top_n = dict(training=2000, testing=1000)
        rpn_post_nms_top_n = dict(training=2000, testing=1000)
        rpn_nms_thresh = 0.7

        rpn = RegionProposalNetwork(
            rpn_anchor_generator, rpn_head,
            rpn_fg_iou_thresh, rpn_bg_iou_thresh,
            rpn_batch_size_per_image, rpn_positive_fraction,
            rpn_pre_nms_top_n, rpn_post_nms_top_n, rpn_nms_thresh)
        return rpn

    def _init_test_roi_heads_faster_rcnn(self):
        out_channels = 256
        num_classes = 91

        box_fg_iou_thresh = 0.5
        box_bg_iou_thresh = 0.5
        box_batch_size_per_image = 512
        box_positive_fraction = 0.25
        bbox_reg_weights = None
        box_score_thresh = 0.05
        box_nms_thresh = 0.5
        box_detections_per_img = 100

        box_roi_pool = ops.MultiScaleRoIAlign(
            featmap_names=['0', '1', '2', '3'],
            output_size=7,
            sampling_ratio=2)

        resolution = box_roi_pool.output_size[0]
        representation_size = 1024
        box_head = TwoMLPHead(
            out_channels * resolution ** 2,
            representation_size)

        representation_size = 1024
        box_predictor = FastRCNNPredictor(
            representation_size,
            num_classes)

        roi_heads = RoIHeads(
            box_roi_pool, box_head, box_predictor,
            box_fg_iou_thresh, box_bg_iou_thresh,
            box_batch_size_per_image, box_positive_fraction,
            bbox_reg_weights,
            box_score_thresh, box_nms_thresh, box_detections_per_img)
        return roi_heads

    def get_features(self, images):
        s0, s1 = images.shape[-2:]
        features = [
            ('0', torch.rand(2, 256, s0 // 4, s1 // 4)),
            ('1', torch.rand(2, 256, s0 // 8, s1 // 8)),
            ('2', torch.rand(2, 256, s0 // 16, s1 // 16)),
            ('3', torch.rand(2, 256, s0 // 32, s1 // 32)),
            ('4', torch.rand(2, 256, s0 // 64, s1 // 64)),
        ]
        features = OrderedDict(features)
        return features

    def test_rpn(self):
        class RPNModule(torch.nn.Module):
            def __init__(self_module, images):
                super(RPNModule, self_module).__init__()
                self_module.rpn = self._init_test_rpn()
                self_module.images = ImageList(images, [i.shape[-2:] for i in images])

            def forward(self_module, features):
                return self_module.rpn(self_module.images, features)

        images = torch.rand(2, 3, 600, 600)
        features = self.get_features(images)
        test_features = self.get_features(images)

        model = RPNModule(images)
        model.eval()
        model(features)
        self.run_model(model, [(features,), (test_features,)], tolerate_small_mismatch=True)

    def test_multi_scale_roi_align(self):

        class TransformModule(torch.nn.Module):
            def __init__(self):
                super(TransformModule, self).__init__()
                self.model = ops.MultiScaleRoIAlign(['feat1', 'feat2'], 3, 2)
                self.image_sizes = [(512, 512)]

            def forward(self, input, boxes):
                return self.model(input, boxes, self.image_sizes)

        i = OrderedDict()
        i['feat1'] = torch.rand(1, 5, 64, 64)
        i['feat2'] = torch.rand(1, 5, 16, 16)
        boxes = torch.rand(6, 4) * 256
        boxes[:, 2:] += boxes[:, :2]

        i1 = OrderedDict()
        i1['feat1'] = torch.rand(1, 5, 64, 64)
        i1['feat2'] = torch.rand(1, 5, 16, 16)
        boxes1 = torch.rand(6, 4) * 256
        boxes1[:, 2:] += boxes1[:, :2]

        self.run_model(TransformModule(), [(i, [boxes],), (i1, [boxes1],)])

    @unittest.skipIf(torch.__version__ < "1.4.", "Disable test if torch version is less than 1.4")
    def test_roi_heads(self):
        class RoiHeadsModule(torch.nn.Module):
            def __init__(self_module, images):
                super(RoiHeadsModule, self_module).__init__()
                self_module.transform = self._init_test_generalized_rcnn_transform()
                self_module.rpn = self._init_test_rpn()
                self_module.roi_heads = self._init_test_roi_heads_faster_rcnn()
                self_module.original_image_sizes = [img.shape[-2:] for img in images]
                self_module.images = ImageList(images, [i.shape[-2:] for i in images])

            def forward(self_module, features):
                proposals, _ = self_module.rpn(self_module.images, features)
                detections, _ = self_module.roi_heads(features, proposals, self_module.images.image_sizes)
                detections = self_module.transform.postprocess(detections,
                                                               self_module.images.image_sizes,
                                                               self_module.original_image_sizes)
                return detections

        images = torch.rand(2, 3, 600, 600)
        features = self.get_features(images)
        test_features = self.get_features(images)

        model = RoiHeadsModule(images)
        model.eval()
        model(features)
        self.run_model(model, [(features,), (test_features,)], tolerate_small_mismatch=True)

    def get_image_from_url(self, url):
        import requests
        import numpy
        from PIL import Image
        from io import BytesIO
        from torchvision import transforms

        data = requests.get(url)
        image = Image.open(BytesIO(data.content)).convert("RGB")
        image = image.resize((800, 1280), Image.BILINEAR)

        to_tensor = transforms.ToTensor()
        return to_tensor(image)

    def get_test_images(self):
        image_url = "http://farm3.staticflickr.com/2469/3915380994_2e611b1779_z.jpg"
        image = self.get_image_from_url(url=image_url)
        image_url2 = "https://pytorch.org/tutorials/_static/img/tv_tutorial/tv_image05.png"
        image2 = self.get_image_from_url(url=image_url2)
        images = [image]
        test_images = [image2]
        return images, test_images

    @unittest.skip("Disable test until Resize opset 11 is implemented in ONNX Runtime")
    @unittest.skipIf(torch.__version__ < "1.4.", "Disable test if torch version is less than 1.4")
    def test_faster_rcnn(self):
        images, test_images = self.get_test_images()

        model = models.detection.faster_rcnn.fasterrcnn_resnet50_fpn(pretrained=True)
        model.eval()
        model(images)
        self.run_model(model, [(images,), (test_images,)])


if __name__ == '__main__':
    unittest.main()

import os
import sys
import inspect
import colorsys
import onnx
import numpy as np
import tensorflow as tf
import keras
from PIL import Image, ImageFont, ImageDraw
from keras import backend as K
from keras.layers import Input
from keras.models import load_model
from keras2onnx import convert_keras
from keras2onnx import set_converter
from keras2onnx.common.onnx_ops import apply_transpose, apply_identity, apply_cast
from keras2onnx.proto import onnx_proto

from os.path import dirname, abspath
yolo3_dir = os.path.join(os.path.dirname(__file__), '../../keras-yolo3')
if os.path.exists(yolo3_dir):
    sys.path.insert(0, yolo3_dir)

import yolo3
from yolo3.model import yolo_body, tiny_yolo_body, yolo_boxes_and_scores
from yolo3.utils import letterbox_image


class YOLOEvaluationLayer(keras.layers.Layer):

    def __init__(self, **kwargs):
        super(YOLOEvaluationLayer, self).__init__()
        self.anchors = np.array(kwargs.get('anchors'))
        self.num_classes = kwargs.get('num_classes')

    def get_config(self):
        config = {
            "anchors": self.anchors,
            "num_classes": self.num_classes,
        }

        return config

    def call(self, inputs, **kwargs):
        """Evaluate YOLO model on given input and return filtered boxes."""
        yolo_outputs = inputs[0:-1]
        input_image_shape = K.squeeze(inputs[-1], axis=0)
        num_layers = len(yolo_outputs)
        anchor_mask = [[6, 7, 8], [3, 4, 5], [0, 1, 2]] if num_layers == 3 else [[3, 4, 5],
                                                                                 [1, 2, 3]]  # default setting
        input_shape = K.shape(yolo_outputs[0])[1:3] * 32
        boxes = []
        box_scores = []
        for l in range(num_layers):
            _boxes, _box_scores = yolo_boxes_and_scores(yolo_outputs[l], self.anchors[anchor_mask[l]], self.num_classes,
                                                        input_shape, input_image_shape)
            boxes.append(_boxes)
            box_scores.append(_box_scores)
        boxes = K.concatenate(boxes, axis=0)
        box_scores = K.concatenate(box_scores, axis=0)
        return [boxes, box_scores]

    def compute_output_shape(self, input_shape):
        assert isinstance(input_shape, list)
        return [(None, 4), (None, None)]


class YOLONMSLayer(keras.layers.Layer):
    def __init__(self, **kwargs):
        super(YOLONMSLayer, self).__init__()
        self.max_boxes = kwargs.get('max_boxes', 20)
        self.score_threshold = kwargs.get('score_threshold', .6)
        self.iou_threshold = kwargs.get('iou_threshold', .5)
        self.num_classes = kwargs.get('num_classes')

    def get_config(self):
        config = {
            "max_boxes": self.max_boxes,
            "score_threshold": self.score_threshold,
            "iou_threshold": self.iou_threshold,
            "num_classes": self.num_classes,
        }

        return config

    def call(self, inputs, **kwargs):
        boxes = inputs[0]
        box_scores = inputs[1]
        box_scores_transpose = tf.transpose(box_scores, perm=[1, 0])
        boxes_number = tf.shape(boxes)[0]
        box_range = tf.range(boxes_number)

        mask = box_scores >= self.score_threshold
        max_boxes_tensor = K.constant(self.max_boxes, dtype='int32')
        classes_ = []
        batch_indexs_ = []
        nms_indexes_ = []
        class_box_range_ = []
        for c in range(self.num_classes):
            class_boxes = tf.boolean_mask(boxes, mask[:, c])
            class_box_scores = tf.boolean_mask(box_scores[:, c], mask[:, c])
            class_box_range = tf.boolean_mask(box_range, mask[:, c])
            nms_index = tf.image.non_max_suppression(
                class_boxes, class_box_scores, max_boxes_tensor, iou_threshold=self.iou_threshold)
            class_box_scores = K.gather(class_box_scores, nms_index)
            class_box_range = K.gather(class_box_range, nms_index)
            classes = K.ones_like(class_box_scores, 'int32') * c
            batch_index = K.zeros_like(class_box_scores, 'int32')
            batch_indexs_.append(batch_index)
            classes_.append(classes)
            nms_indexes_.append(nms_index)
            class_box_range_.append(class_box_range)

        classes_ = K.concatenate(classes_, axis=0)
        batch_indexs_ = K.concatenate(batch_indexs_, axis=0)
        class_box_range_ = K.concatenate(class_box_range_, axis=0)

        boxes_1 = tf.expand_dims(boxes, 0)
        classes_1 = tf.expand_dims(classes_, 1)
        batch_indexs_ = tf.expand_dims(batch_indexs_, 1)
        class_box_range_ = tf.expand_dims(class_box_range_, 1)
        box_scores_transpose_1 = tf.expand_dims(box_scores_transpose, 0)
        nms_final_ = K.concatenate([batch_indexs_, classes_1, class_box_range_], axis=1)
        nms_final_1 = tf.expand_dims(nms_final_, 0)
        return [boxes_1, box_scores_transpose_1, nms_final_1]

    def compute_output_shape(self, input_shape):
        assert isinstance(input_shape, list)
        return [(None, None, 4), (None, self.num_classes, None), (None, None, 3)]


class YOLO(object):
    def __init__(self, model_path='model_data/yolo.h5', anchors_path='model_data/yolo_anchors.txt', yolo3_dir=None):
        self.yolo3_dir = yolo3_dir
        self.model_path = model_path
        self.anchors_path = anchors_path
        self.classes_path = 'model_data/coco_classes.txt'
        self.score = 0.3
        self.iou = 0.45
        self.class_names = self._get_class()
        self.anchors = self._get_anchors()
        self.sess = K.get_session()
        self.model_image_size = (416, 416)  # fixed size or (None, None), hw
        self.session = None
        self.final_model = None

        # Generate colors for drawing bounding boxes.
        hsv_tuples = [(x / len(self.class_names), 1., 1.)
                      for x in range(len(self.class_names))]
        self.colors = list(map(lambda x: colorsys.hsv_to_rgb(*x), hsv_tuples))
        self.colors = list(
            map(lambda x: (int(x[0] * 255), int(x[1] * 255), int(x[2] * 255)),
                self.colors))
        np.random.seed(10101)  # Fixed seed for consistent colors across runs.
        np.random.shuffle(self.colors)  # Shuffle colors to decorrelate adjacent classes.
        np.random.seed(None)  # Reset seed to default.
        K.set_learning_phase(0)

    @staticmethod
    def _get_data_path(name, yolo3_dir):
        path = os.path.expanduser(name)
        if not os.path.isabs(path):
            if yolo3_dir is None:
                yolo3_dir = os.path.dirname(inspect.getabsfile(yolo3))
            path = os.path.join(yolo3_dir, os.path.pardir, path)
        return path

    def _get_class(self):
        classes_path = self._get_data_path(self.classes_path, self.yolo3_dir)
        with open(classes_path) as f:
            class_names = f.readlines()
        class_names = [c.strip() for c in class_names]
        return class_names

    def _get_anchors(self):
        anchors_path = self._get_data_path(self.anchors_path, self.yolo3_dir)
        with open(anchors_path) as f:
            anchors = f.readline()
        anchors = [float(x) for x in anchors.split(',')]
        return np.array(anchors).reshape(-1, 2)

    def load_model(self, yolo_weights=None):
        model_path = self._get_data_path(self.model_path, self.yolo3_dir)
        assert model_path.endswith('.h5'), 'Keras model or weights must be a .h5 file.'
        if yolo_weights is None:
            # Load model, or construct model and load weights.
            num_anchors = len(self.anchors)
            num_classes = len(self.class_names)
            is_tiny_version = num_anchors == 6  # default setting

            try:
                self.yolo_model = load_model(model_path, compile=False)
            except:
                self.yolo_model = tiny_yolo_body(Input(shape=(None, None, 3)), num_anchors // 2, num_classes) \
                    if is_tiny_version else yolo_body(Input(shape=(None, None, 3)), num_anchors // 3, num_classes)
                self.yolo_model.load_weights(self.model_path)  # make sure model, anchors and classes match
            else:
                assert self.yolo_model.layers[-1].output_shape[-1] == \
                    num_anchors / len(self.yolo_model.output) * (num_classes + 5), \
                    'Mismatch between model and given anchor and class sizes'
        else:
            self.yolo_model = yolo_weights

        input_image_shape = keras.Input(shape=(2,), name='image_shape')
        image_input = keras.Input((None, None, 3), dtype='float32', name='input_1')
        y = list(self.yolo_model(image_input))
        y.append(input_image_shape)

        boxes, box_scores = \
            YOLOEvaluationLayer(anchors=self.anchors, num_classes=len(self.class_names))(inputs=y)

        out_boxes, out_scores, out_indices = \
            YOLONMSLayer(anchors=self.anchors, num_classes=len(self.class_names))(
                inputs=[boxes, box_scores])
        self.final_model = keras.Model(inputs=[image_input, input_image_shape],
                                       outputs=[out_boxes, out_scores, out_indices])

        self.final_model.save('final_model.h5')
        print('{} model, anchors, and classes loaded.'.format(model_path))

    def prepare_keras_data(self, image):
        if self.model_image_size != (None, None):
            assert self.model_image_size[0] % 32 == 0, 'Multiples of 32 required'
            assert self.model_image_size[1] % 32 == 0, 'Multiples of 32 required'
            boxed_image = letterbox_image(image, tuple(reversed(self.model_image_size)))
        else:
            new_image_size = (image.width - (image.width % 32),
                              image.height - (image.height % 32))
            boxed_image = letterbox_image(image, new_image_size)
        image_data = np.array(boxed_image, dtype='float32')
        image_data /= 255.
        image_data = np.expand_dims(image_data, 0) # Add batch dimension.
        return image_data

    def detect_with_onnx(self, image):
        self.load_model()
        image_data = self.prepare_keras_data(image)
        all_boxes_k, all_scores_k, indices_k = self.final_model.predict([image_data, np.array([image.size[1], image.size[0]], dtype='float32').reshape(1, 2)])

        image_data_onnx = np.transpose(image_data, [0, 3, 1, 2])
        feed_f = dict(zip(['input_1', 'image_shape'],
                          (image_data_onnx, np.array([image.size[1], image.size[0]], dtype='float32').reshape(1, 2))))
        all_boxes, all_scores, indices = self.session.run(None, input_feed=feed_f)
        print("indices: ", indices)
        out_boxes, out_scores, out_classes = [], [], []
        for idx_ in indices:
            print("idx_: ", idx_)
            out_classes.append(idx_[1])
            out_scores.append(all_scores[tuple(idx_)])
            idx_1 = (idx_[0], idx_[2])
            out_boxes.append(all_boxes[idx_1])

        font = ImageFont.truetype(font=self._get_data_path('font/FiraMono-Medium.otf', self.yolo3_dir),
                                  size=np.floor(3e-2 * image.size[1] + 0.5).astype('int32'))
        thickness = (image.size[0] + image.size[1]) // 300

        for i, c in reversed(list(enumerate(out_classes))):
            predicted_class = self.class_names[c]
            box = out_boxes[i]
            score = out_scores[i]

            label = '{} {:.2f}'.format(predicted_class, score)
            draw = ImageDraw.Draw(image)
            label_size = draw.textsize(label, font)

            top, left, bottom, right = box
            top = max(0, np.floor(top + 0.5).astype('int32'))
            left = max(0, np.floor(left + 0.5).astype('int32'))
            bottom = min(image.size[1], np.floor(bottom + 0.5).astype('int32'))
            right = min(image.size[0], np.floor(right + 0.5).astype('int32'))

            if top - label_size[1] >= 0:
                text_origin = np.array([left, top - label_size[1]])
            else:
                text_origin = np.array([left, top + 1])

            for i in range(thickness):
                draw.rectangle(
                    [left + i, top + i, right - i, bottom - i],
                    outline=self.colors[c])
            draw.rectangle(
                [tuple(text_origin), tuple(text_origin + label_size)],
                fill=self.colors[c])
            draw.text(text_origin, label, fill=(0, 0, 0), font=font)
            del draw

        return image


def detect_img(yolo, img_url, model_file_name):
    import onnxruntime
    image = Image.open(img_url)
    print("####################  img_url: ", img_url)
    yolo.session = onnxruntime.InferenceSession(model_file_name)

    r_image = yolo.detect_with_onnx(image)
    n_ext = img_url.rindex('.')
    score_file = img_url[0:n_ext] + '_score' + img_url[n_ext:]
    r_image.save(score_file, "JPEG")


def convert_NMSLayer(scope, operator, container):
    # type: (keras2onnx.common.InterimContext, keras2onnx.common.Operator, keras2onnx.common.OnnxObjectContainer) -> None
    box_transpose = scope.get_unique_variable_name(operator.inputs[0].full_name + '_tx')
    score_transpose = scope.get_unique_variable_name(operator.inputs[1].full_name + '_tx')

    apply_identity(scope, operator.inputs[0].full_name, box_transpose, container)
    apply_transpose(scope, operator.inputs[1].full_name, score_transpose, container, perm=[1, 0])

    box_batch = scope.get_unique_variable_name(operator.inputs[0].full_name + '_btc')
    score_batch = scope.get_unique_variable_name(operator.inputs[1].full_name + '_btc')

    container.add_node("Unsqueeze", box_transpose,
                       box_batch, op_version=operator.target_opset, axes=[0])
    container.add_node("Unsqueeze", score_transpose,
                       score_batch, op_version=operator.target_opset, axes=[0])

    layer = operator.raw_operator  # type: YOLONMSLayer

    max_output_size = scope.get_unique_variable_name('max_output_size')
    iou_threshold = scope.get_unique_variable_name('iou_threshold')
    score_threshold = scope.get_unique_variable_name('layer.score_threshold')

    container.add_initializer(max_output_size, onnx_proto.TensorProto.INT64,
                              [], [layer.max_boxes])
    container.add_initializer(iou_threshold, onnx_proto.TensorProto.FLOAT,
                              [], [layer.iou_threshold])
    container.add_initializer(score_threshold, onnx_proto.TensorProto.FLOAT,
                              [], [layer.score_threshold])

    cast_name = scope.get_unique_variable_name('casted')
    nms_node = next((nd_ for nd_ in operator.nodelist if nd_.type == 'NonMaxSuppressionV3'), operator.nodelist[0])
    container.add_node("NonMaxSuppression",
                       [box_batch, score_batch, max_output_size, iou_threshold, score_threshold],
                       cast_name,
                       op_version=operator.target_opset,
                       name=nms_node.name)

    cast_batch = scope.get_unique_variable_name(operator.output_full_names[2] + '_btc')
    container.add_node("Unsqueeze", cast_name,
                       cast_batch, op_version=operator.target_opset, axes=[0])
    apply_cast(scope, cast_batch, operator.output_full_names[2], container, to=onnx_proto.TensorProto.INT32)

    apply_identity(scope, box_batch, operator.output_full_names[0], container)
    apply_identity(scope, score_batch, operator.output_full_names[1], container)


set_converter(YOLONMSLayer, convert_NMSLayer)


def convert_model(yolo, model_file_name, target_opset):
    yolo.load_model()
    onnxmodel = convert_keras(yolo.final_model, target_opset=target_opset, channel_first_inputs=['input_1'])
    onnx.save_model(onnxmodel, model_file_name)
    return onnxmodel


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Need an image file for object detection.")
        exit(-1)

    target_opset = 10

    model_file_name = 'model_data/yolov3.onnx'
    model_path = 'model_data/yolo.h5'  # model path or trained weights path
    anchors_path = 'model_data/yolo_anchors.txt'
    '''
    # For tiny yolov3 case, use:
    model_file_name = 'model_data/yolov3-tiny.onnx'
    model_path = 'model_data/yolo-tiny.h5'
    anchors_path = 'model_data/tiny_yolo_anchors.txt'
    '''

    # if not os.path.exists(model_file_name):
    #     onnxmodel = convert_model(YOLO(model_path, anchors_path), model_file_name, target_opset)

    detect_img(YOLO(), sys.argv[1], model_file_name)

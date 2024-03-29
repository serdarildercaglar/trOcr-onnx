import os
import time
from typing import Optional, Tuple

import torch
from PIL import Image

import onnxruntime as onnxrt
import requests
from transformers import AutoConfig, AutoModelForVision2Seq, TrOCRProcessor, VisionEncoderDecoderModel
from transformers.generation.utils import GenerationMixin
from transformers.modeling_outputs import BaseModelOutput, Seq2SeqLMOutput



model_name = "microsoft/trocr-base-handwritten"
device = torch.device("cuda")

class ORTEncoder(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.main_input_name = "pixel_values"
        self._device = device
        self.session = onnxrt.InferenceSession(
            "/home/forest/Desktop/trOcr-onnx/ocr_model/encoder_model.onnx", providers=["CPUExecutionProvider"]
        )
        self.input_names = {input_key.name: idx for idx, input_key in enumerate(self.session.get_inputs())}
        self.output_names = {output_key.name: idx for idx, output_key in enumerate(self.session.get_outputs())}

    def forward(
        self,
        pixel_values: torch.FloatTensor,
        **kwargs,
    ) -> BaseModelOutput:

        onnx_inputs = {"pixel_values": pixel_values.cpu().detach().numpy()}

        # Run inference
        outputs = self.session.run(None, onnx_inputs)
        last_hidden_state = torch.from_numpy(outputs[self.output_names["last_hidden_state"]]).to(self._device)

        return BaseModelOutput(last_hidden_state=last_hidden_state)


class ORTDecoder(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self._device = device

        self.session = onnxrt.InferenceSession(
            "/home/forest/Desktop/trOcr-onnx/ocr_model/decoder_model.onnx", providers=["CPUExecutionProvider"]
        )

        self.input_names = {input_key.name: idx for idx, input_key in enumerate(self.session.get_inputs())}
        self.output_names = {output_key.name: idx for idx, output_key in enumerate(self.session.get_outputs())}

    def forward(
        self,
        input_ids: torch.LongTensor,
        attention_mask: torch.LongTensor,
        encoder_hidden_states: torch.FloatTensor,
    ) -> Seq2SeqLMOutput:

        onnx_inputs = {
            "input_ids": input_ids.cpu().detach().numpy(),
        }

        if "attention_mask" in self.input_names:
            onnx_inputs["attention_mask"] = attention_mask.cpu().detach().numpy()

        # Add the encoder_hidden_states inputs when needed
        if "encoder_hidden_states" in self.input_names:
            onnx_inputs["encoder_hidden_states"] = encoder_hidden_states.cpu().detach().numpy()

        # Run inference
        outputs = self.session.run(None, onnx_inputs)

        logits = torch.from_numpy(outputs[self.output_names["logits"]]).to(self._device)
        return Seq2SeqLMOutput(logits=logits)

    def prepare_inputs_for_generation(self, input_ids, attention_mask=None, encoder_hidden_states=None, **kwargs):
        if attention_mask is None:
            attention_mask = input_ids.new_ones(input_ids.shape)

        return {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "encoder_hidden_states": encoder_hidden_states,
        }


class ORTModelForVision2Seq(VisionEncoderDecoderModel, GenerationMixin):
    def __init__(self, *args, **kwargs):
        config = AutoConfig.from_pretrained(model_name)
        super().__init__(config)
        self._device = device

        self.encoder = ORTEncoder()
        self.decoder = ORTDecoder()

    def forward(
        self,
        pixel_values: Optional[torch.FloatTensor] = None,
        decoder_input_ids: Optional[torch.LongTensor] = None,
        encoder_outputs: Optional[Tuple[Tuple[torch.Tensor]]] = None,
        **kwargs,
    ) -> Seq2SeqLMOutput:
        if encoder_outputs is None:
            encoder_outputs = self.encoder(pixel_values=pixel_values.to(device))

        # Decode
        decoder_attention_mask = decoder_input_ids.new_ones(decoder_input_ids.shape)
        decoder_outputs = self.decoder(
            input_ids=decoder_input_ids,
            attention_mask=decoder_attention_mask,
            encoder_hidden_states=encoder_outputs.last_hidden_state,
        )

        return Seq2SeqLMOutput(
            logits=decoder_outputs.logits,
        )

    def prepare_inputs_for_generation(self, input_ids, attention_mask=None, encoder_outputs=None, **kwargs):

        return {
            "decoder_input_ids": input_ids,
            "decoder_atttention_mask": input_ids,
            "encoder_outputs": encoder_outputs,
        }

    @property
    def device(self) -> torch.device:
        return self._device

    @device.setter
    def device(self, value: torch.device):
        self._device = value

    def to(self, device):
        self.device = device
        return self



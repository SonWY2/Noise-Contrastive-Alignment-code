# Copyright 2022 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Union

import torch
from torch.nn.utils.rnn import pad_sequence
from transformers import PreTrainedModel, PreTrainedTokenizerBase

DEFAULT_CHAT_TEMPLATE = "{% for message in messages %}\n{% if message['role'] == 'user' %}\n{{ '<|user|>\n' + message['content'] + eos_token }}\n{% elif message['role'] == 'system' %}\n{{ '<|system|>\n' + message['content'] + eos_token }}\n{% elif message['role'] == 'assistant' %}\n{{ '<|assistant|>\n'  + message['content'] + eos_token }}\n{% endif %}\n{% if loop.last and add_generation_prompt %}\n{{ '<|assistant|>' }}\n{% endif %}\n{% endfor %}"


@dataclass
class NCADataCollatorWithPadding:
    r"""
    Contrast DataCollator class that pads the inputs to the maximum length of the batch.
    Args:
        tokenizer (`PreTrainedTokenizerBase`):
            The tokenizer used for encoding the data.
        model (Optional[`PreTrainedModel`]):
            The model that is being trained. If set and has the *prepare_decoder_input_ids_from_labels*, use it to
            prepare the *decoder_input_ids*.
        padding (`Union[bool, str, `PaddingStrategy`]`, `optional`, defaults to `True`):
            padding_strategy to pass to the tokenizer.
        max_length (`Optional[int]`, `optional`, defaults to `None`):
            The maximum length of the sequence to be processed.
        max_prompt_length (`Optional[int]`, `optional`, defaults to `None`):
            The maximum length of the prompt to be processed.
        label_pad_token_id (`int`, defaults to -100):
            The label used for masking.
        padding_value (`int`, defaults to 0):
            The value used for padding.
        is_encoder_decoder (`Optional[bool]`, `optional`, defaults to `None`):
            Whether or not you model has an encoder_decoder architecture.
        max_target_length (`Optional[int]`, `optional`, defaults to `None`):
            The maximum length of the target to be processed. Only useful for encoder-decoder architectures.
        truncation_mode: (`str`, defaults to "keep_end"):
            The truncation mode to use when truncating the prompt.
    """
    tokenizer: PreTrainedTokenizerBase
    model: Optional[PreTrainedModel] = None
    padding: Union[bool, str] = True
    max_length: Optional[int] = None
    max_prompt_length: Optional[int] = None
    label_pad_token_id: int = -100
    padding_value: int = 0
    truncation_mode: str = "keep_end"
    is_encoder_decoder: Optional[bool] = False
    max_target_length: Optional[int] = None
    optimize_prompt: Optional[bool] = False

    def tokenize_batch_element(
        self,
        item,
    ) -> Dict:
        """Tokenize a single batch element.

        At this stage, we don't convert to PyTorch tensors yet; we just handle the truncation
            in case the prompt + chosen or prompt + rejected responses is/are too long. First
            we truncate the prompt; if we're still too long, we truncate the chosen/rejected.

        We also create the labels for the chosen/rejected responses, which are of length equal to
            the sum of the length of the prompt and the chosen/rejected response, with
            label_pad_token_id  for the prompt tokens.
        """
        batch = {}

        if not self.is_encoder_decoder:
            prompt_tokens = self.tokenizer(item["instruction"], add_special_tokens=False)

            # A0_tokens = self.tokenizer(item["A0"], add_special_tokens=False)#{"input_ids": "attention_mask":}
            # A1_tokens = self.tokenizer(item["A1"], add_special_tokens=False)#{"input_ids": "attention_mask":}
            # A2_tokens = self.tokenizer(item["A2"], add_special_tokens=False)#{"input_ids": "attention_mask":}
            # A3_tokens = self.tokenizer(item["A3"], add_special_tokens=False)#{"input_ids": "attention_mask":}
            # prompt_tokens = self.tokenizer(item["prompt"], add_special_tokens=False)

            eos_token_id = self.tokenizer.eos_token_id
            # Get indices in list prompt_tokens["input_ids"] that equals the EOS token (often 0)
            eos_indices_prompt = [i for i, x in enumerate(prompt_tokens["input_ids"]) if x == eos_token_id]
            # attention mask these indices to eos_token_id
            new_attention_mask = [
                0 if i in eos_indices_prompt else p for i, p in enumerate(prompt_tokens["attention_mask"])
            ]
            prompt_tokens["attention_mask"] = new_attention_mask
            
            response_tokens = [self.tokenizer([{"role":"assistant", "content":x}], add_special_tokens=False) for x in item['response']]
            longer_response_length = -1
            for i, a_obj in enumerate(response_tokens):
                eos_indices_A = [i for i, x in enumerate(a_obj["input_ids"]) if x == eos_token_id]
                new_attention_mask_c = [
                    0 if i in eos_indices_A else p for i, p in enumerate(a_obj["attention_mask"])
                ]
                a_obj["attention_mask"] = new_attention_mask_c
                a_obj["input_ids"].append(self.tokenizer.eos_token_id) # prompt is not added with eos finish
                a_obj["attention_mask"].append(1)
                longer_response_length = max(longer_response_length, len(a_obj["input_ids"]))


            if len(prompt_tokens["input_ids"]) + longer_response_length > self.max_length:#
                if self.truncation_mode == "keep_start":
                    prompt_tokens = {k: v[: self.max_prompt_length] for k, v in prompt_tokens.items()}
                elif self.truncation_mode == "keep_end":
                    prompt_tokens = {k: v[-self.max_prompt_length :] for k, v in prompt_tokens.items()}
                else:
                    raise ValueError(f"Unknown truncation mode: {self.truncation_mode}")

            # if that's still too long, truncate the response
            if len(prompt_tokens["input_ids"]) + longer_response_length > self.max_length:
                for i, a_obj in enumerate(response_tokens):
                    response_tokens[i] = {k: v[: self.max_length - self.max_prompt_length] for k, v in a_obj.items()}
            
            sequence_objs = []
            for a_obj in response_tokens:
                a_sequence_obj = {k: prompt_tokens[k] + a_obj[k] for k in a_obj}
                a_sequence_obj['labels'] = a_sequence_obj["input_ids"][:]
            
                if not self.optimize_prompt:
                    a_sequence_obj["labels"][: len(prompt_tokens["input_ids"])] = [self.label_pad_token_id] * len(
                            prompt_tokens["input_ids"]
                    )

                sequence_objs.append(a_sequence_obj)

            for i, a_seq_obj in enumerate(sequence_objs):
                for type_key, tokens in a_seq_obj.items():
                    if type_key == "token_type_ids":
                        continue
                    batch[f"A{i}_{type_key}"] = tokens 
            for type_key, tokens in prompt_tokens.items():
                if type_key == "token_type_ids":
                    continue
                batch[f"prompt_{type_key}"] = tokens

        else:
            raise NotImplementedError

        batch["prompt"] = item["instruction"]
        reward_scale_factor = 10 if sum(item['rewards']) / len(item['rewards']) <= 1.0 else 1
        for i, (resp, reward) in enumerate(zip(response_tokens, item['rewards'])):
            batch[f"A{i}"] = [{'role':'user', "content":item["instruction"]}] + resp
            batch[f"A{i}_response_only"] = resp
            batch[f"A{i}_score"] = reward * reward_scale_factor
        
        return batch

    def collate(self, batch):
        # first, pad everything to the same length
        padded_batch = {}
        for k in batch[0].keys():
            if k.endswith("_input_ids") or k.endswith("_attention_mask") or k.endswith("_labels"):
                if self.is_encoder_decoder:
                    raise NotImplementedError
                else:
                    # adapted from https://stackoverflow.com/questions/73256206
                    # if "prompt" in k:
                    if "instruction" in k:
                        to_pad = [torch.LongTensor(ex[k][::-1]) for ex in batch]
                    else:
                        to_pad = [torch.LongTensor(ex[k]) for ex in batch]
                    if k.endswith("_input_ids"):
                        padding_value = self.tokenizer.pad_token_id
                    elif k.endswith("_labels"):
                        padding_value = self.label_pad_token_id
                    elif k.endswith("_attention_mask"):
                        padding_value = self.padding_value
                    else:
                        raise ValueError(f"Unexpected key in batch '{k}'")

                    padded_batch[k] = pad_sequence(to_pad, batch_first=True, padding_value=padding_value)
                    # for the prompt, flip back so padding is on left side
                    # if "prompt" in k:
                    if "instruction" in k:
                        padded_batch[k] = padded_batch[k].flip(dims=[1])
            elif k.endswith("_score"):
                padded_batch[k] = torch.FloatTensor([ex[k] for ex in batch])
            else:
                padded_batch[k] = [ex[k] for ex in batch]

        return padded_batch

    def __call__(self, features: List[Dict[str, Any]]) -> Dict[str, Any]:
        tokenized_batch = []

        for feature in features:
            batch_element = self.tokenize_batch_element(feature)
            tokenized_batch.append(batch_element)

        # return collated batch
        return self.collate(tokenized_batch)

def apply_chat_template(
    example, tokenizer, task: Literal["sft", "generation", "rm", "dpo", "reward"] = "sft", assistant_prefix="<|assistant|>\n"
):
    def _strip_prefix(s, pattern):
        # Use re.escape to escape any special characters in the pattern
        return re.sub(f"^{re.escape(pattern)}", "", s)

    if task in ["sft", "generation"]:
        messages = example["messages"]
        # We add an empty system message if there is none
        if messages[0]["role"] != "system":
            messages.insert(0, {"role": "system", "content": ""})
        example["text"] = tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True if task == "generation" else False
        )
    elif task == "rm":
        if all(k in example.keys() for k in ("chosen", "rejected")):
            chosen_messages = example["chosen"]
            rejected_messages = example["rejected"]
            # We add an empty system message if there is none
            if chosen_messages[0]["role"] != "system":
                chosen_messages.insert(0, {"role": "system", "content": ""})
            if rejected_messages[0]["role"] != "system":
                rejected_messages.insert(0, {"role": "system", "content": ""})
            example["text_chosen"] = tokenizer.apply_chat_template(chosen_messages, tokenize=False)
            example["text_rejected"] = tokenizer.apply_chat_template(rejected_messages, tokenize=False)
        else:
            raise ValueError(
                f"Could not format example as dialogue for `rm` task! Require `[chosen, rejected]` keys but found {list(example.keys())}"
            )
    elif task == "dpo":
        if all(k in example.keys() for k in ("chosen", "rejected")):
            # Compared to reward modeling, we filter out the prompt, so the text is everything after the last assistant token
            prompt_messages = [[msg for msg in example["chosen"] if msg["role"] == "user"][0]]
            # Insert system message
            if example["chosen"][0]["role"] != "system":
                prompt_messages.insert(0, {"role": "system", "content": ""})
            else:
                prompt_messages.insert(0, example["chosen"][0])
            # TODO: handle case where chosen/rejected also have system messages
            chosen_messages = example["chosen"][1:]
            rejected_messages = example["rejected"][1:]
            example["text_chosen"] = tokenizer.apply_chat_template(chosen_messages, tokenize=False)
            example["text_rejected"] = tokenizer.apply_chat_template(rejected_messages, tokenize=False)
            example["text_prompt"] = tokenizer.apply_chat_template(
                prompt_messages, tokenize=False, add_generation_prompt=True
            )
        elif all(k in example.keys() for k in ("instruction", "real_response", "fake_response")):
            prompt_messages = [{'role':'user', 'content': example['instruction']}]
            system_content = example.get('system', '')
            if not system_content:
                prompt_messages.insert(0, {"role": "system", "content": ""})#inner voice says nothing
            else:
                prompt_messages.insert(0, {'role': 'system', 'content': system_content})
            chosen_messages = [{'role':'user', 'content': example["real_response"]}]
            rejected_messages = [{'role':'user', 'content': example["fake_response"]}]
            example["text_chosen"] = tokenizer.apply_chat_template(chosen_messages, tokenize=False)
            example["text_rejected"] = tokenizer.apply_chat_template(rejected_messages, tokenize=False)
            example["text_prompt"] = tokenizer.apply_chat_template(
                prompt_messages, tokenize=False, add_generation_prompt=True
            )
        else:
            raise ValueError(
                f"task=dpo should have (chosen:list[dict], rejected:list[dict]) or (instruction:str, real_response:str, fake_respnse:str), {example.keys()=}"
            )

        example["text_chosen"] = _strip_prefix(example["text_chosen"], assistant_prefix)
        example["text_rejected"] = _strip_prefix(example["text_rejected"], assistant_prefix)
    elif task == "reward":
        # # TODO remove dummy implementation and support arbitrary K number 
        # if all(k in example.keys() for k in ("A0", "A1","A2", "A3")):
            # prompt_messages = [[msg for msg in example["A0"] if msg["role"] == "user"][0]] #user fisrt query
        k = len(example['response'])
        if all(k in example_keys() for k in ("instruction", "response", "rewards")):
            prompt_messages = [{'role':'user', 'content': example['instruction']}]
            # Compared to reward modeling, we filter out the prompt, so the text is everything after the last assistant token
            # Insert system message  ensure the first thing for  prompt_messages is system voice
            system_content = example.get('system', '')
            if not system_content:
                prompt_messages.insert(0, {"role": "system", "content": ""})#inner voice says nothing
            else:
                prompt_messages.insert(0, {'role': 'system', 'content': system_content})
            # TODO: handle case where chosen/rejected also have system messages
            responses = example['response']
            for i in range(k):
                msg = [{'role': 'assistant', 'content': example['response'][i]}]
                example[f"text_A{i}"] = tokenizer.apply_chat_template(msg, tokenize=False)
                example[f"text_A{i}"] = _strip_prefix(example[f"text_A{i}"], assistant_prefix)

            example["text_prompt"] = tokenizer.apply_chat_template(
                prompt_messages, tokenize=False, add_generation_prompt=True
            )
    else:
        raise ValueError(
            f"Could not format example as dialogue for `dpo` task! Require `[chosen, rejected]` keys but found {list(example.keys())}"
        )
    return example
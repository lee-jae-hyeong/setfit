import os
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple, Union


# Google Colab runs on Python 3.7, so we need this to be compatible
try:
    from typing import Literal
except ImportError:
    from typing_extensions import Literal

import joblib
import numpy as np
import requests
import torch
from huggingface_hub import PyTorchModelHubMixin, hf_hub_download
from sentence_transformers import InputExample, SentenceTransformer, models
from sklearn.linear_model import LogisticRegression
from sklearn.multiclass import OneVsRestClassifier
from sklearn.multioutput import ClassifierChain, MultiOutputClassifier
from torch import nn
from torch.utils.data import DataLoader
from tqdm.auto import tqdm, trange

from . import logging
from .data import SetFitDataset


if TYPE_CHECKING:
    from numpy import ndarray


logging.set_verbosity_info()
logger = logging.get_logger(__name__)

MODEL_HEAD_NAME = "model_head.pkl"

MODEL_CARD_TEMPLATE = """---
license: apache-2.0
tags:
- setfit
- sentence-transformers
- text-classification
pipeline_tag: text-classification
---

# {model_name}

This is a [SetFit model](https://github.com/huggingface/setfit) that can be used for text classification. \
The model has been trained using an efficient few-shot learning technique that involves:

1. Fine-tuning a [Sentence Transformer](https://www.sbert.net) with contrastive learning.
2. Training a classification head with features from the fine-tuned Sentence Transformer.

## Usage

To use this model for inference, first install the SetFit library:

```bash
python -m pip install setfit
```

You can then run inference as follows:

```python
from setfit import SetFitModel

# Download from Hub and run inference
model = SetFitModel.from_pretrained("{model_name}")
# Run inference
preds = model(["i loved the spiderman movie!", "pineapple on pizza is the worst 🤮"])
```

## BibTeX entry and citation info

```bibtex
@article{{https://doi.org/10.48550/arxiv.2209.11055,
doi = {{10.48550/ARXIV.2209.11055}},
url = {{https://arxiv.org/abs/2209.11055}},
author = {{Tunstall, Lewis and Reimers, Nils and Jo, Unso Eun Seo and Bates, Luke and Korat, Daniel and Wasserblat, Moshe and Pereg, Oren}},
keywords = {{Computation and Language (cs.CL), FOS: Computer and information sciences, FOS: Computer and information sciences}},
title = {{Efficient Few-Shot Learning Without Prompts}},
publisher = {{arXiv}},
year = {{2022}},
copyright = {{Creative Commons Attribution 4.0 International}}
}}
```
"""


class SetFitHead(models.Dense):
    """
    A SetFit head that supports multi-class classification for end-to-end training.
    Binary classification is treated as 2-class classification.

    To be compatible with Sentence Transformers, we inherit `Dense` from:
    https://github.com/UKPLab/sentence-transformers/blob/master/sentence_transformers/models/Dense.py

    Args:
        in_features (`int`, *optional*):
            The embedding dimension from the output of the SetFit body. If `None`, defaults to `LazyLinear`.
        out_features (`int`, defaults to `2`):
            The number of targets. If set `out_features` to 1 for binary classification, it will be changed to 2 as 2-class classification.
        temperature (`float`, defaults to `1.0`):
            A logits' scaling factor. Higher values makes the model less confident and higher values makes
            it more confident.
        eps (`float`, defaults to `1e-5`):
            A value for numerical stability when scaling logits.
        bias (`bool`, *optional*, defaults to `True`):
            Whether to add bias to the head.
        device (`torch.device`, str, *optional*):
            The device the model will be sent to. If `None`, will check whether GPU is available.
        multitarget (`bool`, defaults to `False`):
            Enable multi-target classification by making `out_features` binary predictions instead
            of a single multinomial prediction.
    """

    def __init__(
        self,
        in_features: Optional[int] = None,
        out_features: int = 2,
        temperature: float = 1.0,
        eps: float = 1e-5,
        bias: bool = True,
        device: Optional[Union[torch.device, str]] = None,
        multitarget: bool = False,
    ) -> None:
        super(models.Dense, self).__init__()  # init on models.Dense's parent: nn.Module

        if out_features == 1:
            logger.warning(
                "Change `out_features` from 1 to 2 since we use `CrossEntropyLoss` for binary classification."
            )
            out_features = 2

        if in_features is not None:
            self.linear = nn.Linear(in_features, out_features, bias=bias)
        else:
            self.linear = nn.LazyLinear(out_features, bias=bias)

        self.in_features = in_features
        self.out_features = out_features
        self.temperature = temperature
        self.eps = eps
        self.bias = bias
        self._device = device or "cuda" if torch.cuda.is_available() else "cpu"
        self.multitarget = multitarget

        self.to(self._device)
        self.apply(self._init_weight)

    def forward(
        self,
        features: Union[Dict[str, torch.Tensor], torch.Tensor],
        temperature: Optional[float] = None,
    ) -> Union[Dict[str, torch.Tensor], Tuple[torch.Tensor]]:
        """
        SetFitHead can accept embeddings in:
        1. Output format (`dict`) from Sentence-Transformers.
        2. Pure `torch.Tensor`.

        Args:
            features (`Dict[str, torch.Tensor]` or `torch.Tensor):
                The embeddings from the encoder. If using `dict` format,
                make sure to store embeddings under the key: 'sentence_embedding'
                and the outputs will be under the key: 'prediction'.
            temperature (`float`, *optional*):
                A logits' scaling factor. Higher values makes the model less
                confident and higher values makes it more confident.
                Will override the temperature given during initialization.
        Returns:
        [`Dict[str, torch.Tensor]` or `Tuple[torch.Tensor]`]
        """
        temperature = temperature or self.temperature
        is_features_dict = False  # whether `features` is dict or not
        if isinstance(features, dict):
            assert "sentence_embedding" in features
            is_features_dict = True
        x = features["sentence_embedding"] if is_features_dict else features
        logits = self.linear(x)
        logits = logits / (temperature + self.eps)
        if self.multitarget:  # multiple targets per item
            probs = torch.sigmoid(logits)
        else:  # one target per item
            probs = nn.functional.softmax(logits, dim=-1)
        if is_features_dict:
            features.update(
                {
                    "logits": logits,
                    "probs": probs,
                }
            )
            return features

        return logits, probs

    def predict_proba(self, x_test: torch.Tensor) -> torch.Tensor:
        self.eval()

        return self(x_test)[1]

    def predict(self, x_test: torch.Tensor) -> torch.Tensor:
        probs = self.predict_proba(x_test)

        if self.multitarget:
            return torch.where(probs >= 0.5, 1, 0)
        return torch.argmax(probs, dim=-1)

    def get_loss_fn(self) -> nn.Module:
        if self.multitarget:  # if sigmoid output
            return torch.nn.BCEWithLogitsLoss()
        return torch.nn.CrossEntropyLoss()

    @property
    def device(self) -> torch.device:
        """
        `torch.device`: The device on which the model is placed.

        Reference from: https://github.com/UKPLab/sentence-transformers/blob/master/sentence_transformers/SentenceTransformer.py#L869
        """
        return next(self.parameters()).device

    def get_config_dict(self) -> Dict[str, Optional[Union[int, float, bool]]]:
        return {
            "in_features": self.in_features,
            "out_features": self.out_features,
            "temperature": self.temperature,
            "bias": self.bias,
            "device": self.device.type,  # store the string of the device, instead of `torch.device`
        }

    @staticmethod
    def _init_weight(module):
        if isinstance(module, nn.Linear):
            nn.init.xavier_uniform_(module.weight)
            if module.bias is not None:
                nn.init.constant_(module.bias, 1e-2)

    def __repr__(self):
        return "SetFitHead({})".format(self.get_config_dict())


@dataclass
class SetFitModel(PyTorchModelHubMixin):
    """A SetFit model with integration to the Hugging Face Hub."""

    def __init__(
        self,
        model_body: Optional[SentenceTransformer] = None,
        model_head: Optional[Union[SetFitHead, LogisticRegression]] = None,
        multi_target_strategy: Optional[str] = None,
        l2_weight: float = 1e-2,
        normalize_embeddings: bool = False,
    ) -> None:
        super(SetFitModel, self).__init__()
        self.model_body = model_body
        self.model_head = model_head

        self.multi_target_strategy = multi_target_strategy
        self.l2_weight = l2_weight

        self.normalize_embeddings = normalize_embeddings

    @property
    def has_differentiable_head(self) -> bool:
        # if False, sklearn is assumed to be used instead
        return isinstance(self.model_head, nn.Module)

    def fit(
        self,
        x_train: List[str],
        y_train: Union[List[int], List[List[int]]],
        num_epochs: int,
        batch_size: Optional[int] = None,
        body_learning_rate: Optional[float] = None,
        head_learning_rate: Optional[float] = None,
        l2_weight: Optional[float] = None,
        max_length: Optional[int] = None,
        show_progress_bar: bool = True,
        end_to_end: bool = False,
    ) -> None:
        if self.has_differentiable_head:  # train with pyTorch
            device = self.model_body.device
            self.model_body.train()
            self.model_head.train()
            if not end_to_end:
                self.freeze("body")

            dataloader = self._prepare_dataloader(x_train, y_train, batch_size, max_length)
            criterion = self.model_head.get_loss_fn()
            optimizer = self._prepare_optimizer(head_learning_rate, body_learning_rate, l2_weight)
            scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=5, gamma=0.5)
            for epoch_idx in trange(num_epochs, desc="Epoch", disable=not show_progress_bar):
                for batch in tqdm(dataloader, desc="Iteration", disable=not show_progress_bar, leave=False):
                    features, labels = batch
                    optimizer.zero_grad()

                    # to model's device
                    features = {k: v.to(device) for k, v in features.items()}
                    labels = labels.to(device)

                    outputs = self.model_body(features)
                    if self.normalize_embeddings:
                        outputs = nn.functional.normalize(outputs, p=2, dim=1)
                    outputs = self.model_head(outputs)
                    logits = outputs["logits"]

                    loss: torch.Tensor = criterion(logits, labels)
                    loss.backward()
                    optimizer.step()

                scheduler.step()

            if not end_to_end:
                self.unfreeze("body")
        else:  # train with sklearn
            embeddings = self.model_body.encode(x_train, normalize_embeddings=self.normalize_embeddings)
            self.model_head.fit(embeddings, y_train)

    def _prepare_dataloader(
        self,
        x_train: List[str],
        y_train: Union[List[int], List[List[int]]],
        batch_size: Optional[int] = None,
        max_length: Optional[int] = None,
        shuffle: bool = True,
    ) -> DataLoader:
        max_acceptable_length = self.model_body.get_max_seq_length()
        if max_length is None:
            max_length = max_acceptable_length
            logger.warning(
                f"The `max_length` is `None`. Using the maximum acceptable length according to the current model body: {max_length}."
            )

        if max_length > max_acceptable_length:
            logger.warning(
                (
                    f"The specified `max_length`: {max_length} is greater than the maximum length of the current model body: {max_acceptable_length}. "
                    f"Using {max_acceptable_length} instead."
                )
            )
            max_length = max_acceptable_length

        dataset = SetFitDataset(
            x_train,
            y_train,
            tokenizer=self.model_body.tokenizer,
            max_length=max_length,
        )
        dataloader = DataLoader(
            dataset,
            batch_size=batch_size,
            collate_fn=dataset.collate_fn,
            shuffle=shuffle,
            pin_memory=True,
        )

        return dataloader

    def _prepare_optimizer(
        self,
        head_learning_rate: float,
        body_learning_rate: Optional[float],
        l2_weight: float,
    ) -> torch.optim.Optimizer:
        body_learning_rate = body_learning_rate or head_learning_rate
        l2_weight = l2_weight or self.l2_weight
        optimizer = torch.optim.AdamW(
            [
                {
                    "params": self.model_body.parameters(),
                    "lr": body_learning_rate,
                    "weight_decay": l2_weight,
                },
                {"params": self.model_head.parameters(), "lr": head_learning_rate, "weight_decay": l2_weight},
            ],
        )

        return optimizer

    def freeze(self, component: Optional[Literal["body", "head"]] = None) -> None:
        if component is None or component == "body":
            self._freeze_or_not(self.model_body, to_freeze=True)

        if (component is None or component == "head") and self.has_differentiable_head:
            self._freeze_or_not(self.model_head, to_freeze=True)

    def unfreeze(
        self, component: Optional[Literal["body", "head"]] = None, keep_body_frozen: Optional[bool] = None
    ) -> None:
        if keep_body_frozen is not None:
            warnings.warn(
                "`keep_body_frozen` is deprecated and will be removed in v2.0.0 of SetFit. "
                'Please either pass "head", "body" or no arguments to unfreeze both.',
                DeprecationWarning,
                stacklevel=2,
            )
            # If the body must stay frozen, only unfreeze the head. Eventually, this entire if-branch
            # can be removed.
            if keep_body_frozen and not component:
                component = "head"

        if component is None or component == "body":
            self._freeze_or_not(self.model_body, to_freeze=False)

        if (component is None or component == "head") and self.has_differentiable_head:
            self._freeze_or_not(self.model_head, to_freeze=False)

    def _freeze_or_not(self, model: nn.Module, to_freeze: bool) -> None:
        for param in model.parameters():
            param.requires_grad = not to_freeze

    def encode(self, inputs: List[str]) -> Union[torch.Tensor, "ndarray"]:
        return self.model_body.encode(
            inputs, normalize_embeddings=self.normalize_embeddings, convert_to_tensor=self.has_differentiable_head
        )

    def predict(self, inputs: List[str], as_numpy: bool = False) -> Union[torch.Tensor, "ndarray"]:
        embeddings = self.encode(inputs)
        outputs = self.model_head.predict(embeddings)

        if as_numpy and self.has_differentiable_head:
            outputs = outputs.detach().cpu().numpy()
        elif not as_numpy and not self.has_differentiable_head:
            outputs = torch.from_numpy(outputs)

        return outputs

    def predict_proba(self, inputs: List[str], as_numpy: bool = False) -> Union[torch.Tensor, "ndarray"]:
        embeddings = self.encode(inputs)
        outputs = self.model_head.predict_proba(embeddings)

        if as_numpy and self.has_differentiable_head:
            outputs = outputs.detach().cpu().numpy()
        elif not as_numpy and not self.has_differentiable_head:
            outputs = torch.from_numpy(outputs)

        return outputs

    def to(self, device: Union[str, torch.device]) -> "SetFitModel":
        """Move this SetFitModel to `device`, and then return `self`. This method does not copy.

        Args:
            device (Union[str, torch.device]): The identifier of the device to move the model to.

        Returns:
            SetFitModel: Returns the original model, but now on the desired device.
        """
        # Note that we must also set _target_device, or any SentenceTransformer.fit() call will reset
        # the body location
        self.model_body._target_device = device if isinstance(device, torch.device) else torch.device(device)
        self.model_body = self.model_body.to(device)

        if self.has_differentiable_head:
            self.model_head = self.model_head.to(device)

        return self

    def create_model_card(self, path: str, model_name: Optional[str] = "SetFit Model") -> None:
        """Creates and saves a model card for a SetFit model.

        Args:
            path (str): The path to save the model card to.
            model_name (str, *optional*): The name of the model. Defaults to `SetFit Model`.
        """
        if not os.path.exists(path):
            os.makedirs(path)

        model_card_content = MODEL_CARD_TEMPLATE.format(model_name=model_name)
        with open(os.path.join(path, "README.md"), "w", encoding="utf-8") as f:
            f.write(model_card_content)

    def __call__(self, inputs):
        return self.predict(inputs)

    def _save_pretrained(self, save_directory: str) -> None:
        self.model_body.save(path=save_directory, create_model_card=False)
        self.create_model_card(path=save_directory, model_name=save_directory)
        joblib.dump(self.model_head, f"{save_directory}/{MODEL_HEAD_NAME}")

    @classmethod
    def _from_pretrained(
        cls,
        model_id: str,
        revision: Optional[str] = None,
        cache_dir: Optional[str] = None,
        force_download: Optional[bool] = None,
        proxies: Optional[Dict] = None,
        resume_download: Optional[bool] = None,
        local_files_only: Optional[bool] = None,
        use_auth_token: Optional[Union[bool, str]] = None,
        multi_target_strategy: Optional[str] = None,
        use_differentiable_head: bool = False,
        normalize_embeddings: bool = False,
        **model_kwargs,
    ) -> "SetFitModel":
        model_body = SentenceTransformer(model_id, cache_folder=cache_dir, use_auth_token=use_auth_token)
        target_device = model_body._target_device
        model_body.to(target_device)  # put `model_body` on the target device

        if os.path.isdir(model_id):
            if MODEL_HEAD_NAME in os.listdir(model_id):
                model_head_file = os.path.join(model_id, MODEL_HEAD_NAME)
            else:
                logger.info(
                    f"{MODEL_HEAD_NAME} not found in {Path(model_id).resolve()},"
                    " initialising classification head with random weights."
                    " You should TRAIN this model on a downstream task to use it for predictions and inference."
                )
                model_head_file = None
        else:
            try:
                model_head_file = hf_hub_download(
                    repo_id=model_id,
                    filename=MODEL_HEAD_NAME,
                    revision=revision,
                    cache_dir=cache_dir,
                    force_download=force_download,
                    proxies=proxies,
                    resume_download=resume_download,
                    use_auth_token=use_auth_token,
                    local_files_only=local_files_only,
                )
            except requests.exceptions.RequestException:
                logger.info(
                    f"{MODEL_HEAD_NAME} not found on HuggingFace Hub, initialising classification head with random weights."
                    " You should TRAIN this model on a downstream task to use it for predictions and inference."
                )
                model_head_file = None

        if model_head_file is not None:
            model_head = joblib.load(model_head_file)
        else:
            head_params = model_kwargs.get("head_params", {})
            if use_differentiable_head:
                if multi_target_strategy is None:
                    use_multitarget = False
                else:
                    if multi_target_strategy in ["one-vs-rest", "multi-output"]:
                        use_multitarget = True
                    else:
                        raise ValueError(
                            f"multi_target_strategy '{multi_target_strategy}' is not supported for differentiable head"
                        )
                # Base `model_head` parameters
                # - get the sentence embedding dimension from the `model_body`
                # - follow the `model_body`, put `model_head` on the target device
                base_head_params = {
                    "in_features": model_body.get_sentence_embedding_dimension(),
                    "device": target_device,
                    "multitarget": use_multitarget,
                }
                model_head = SetFitHead(**{**head_params, **base_head_params})
            else:
                clf = LogisticRegression(**head_params)
                if multi_target_strategy is not None:
                    if multi_target_strategy == "one-vs-rest":
                        multilabel_classifier = OneVsRestClassifier(clf)
                    elif multi_target_strategy == "multi-output":
                        multilabel_classifier = MultiOutputClassifier(clf)
                    elif multi_target_strategy == "classifier-chain":
                        multilabel_classifier = ClassifierChain(clf)
                    else:
                        raise ValueError(f"multi_target_strategy {multi_target_strategy} is not supported.")

                    model_head = multilabel_classifier
                else:
                    model_head = clf

        return SetFitModel(
            model_body=model_body,
            model_head=model_head,
            multi_target_strategy=multi_target_strategy,
            normalize_embeddings=normalize_embeddings,
        )


def sentence_pairs_generation(sentences, labels, pairs):
    # Initialize two empty lists to hold the (sentence, sentence) pairs and
    # labels to indicate if a pair is positive or negative

    num_classes = np.unique(labels)
    idx = [np.where(labels == i)[0] for i in num_classes]

    for first_idx in range(len(sentences)):
        current_sentence = sentences[first_idx]
        label = labels[first_idx]
        second_idx = np.random.choice(idx[np.where(num_classes == label)[0][0]])
        positive_sentence = sentences[second_idx]
        # Prepare a positive pair and update the sentences and labels
        # lists, respectively
        pairs.append(InputExample(texts=[current_sentence, positive_sentence], label=1.0))

        negative_idx = np.where(labels != label)[0]
        negative_sentence = sentences[np.random.choice(negative_idx)]
        # Prepare a negative pair of sentences and update our lists
        pairs.append(InputExample(texts=[current_sentence, negative_sentence], label=0.0))
    # Return a 2-tuple of our sentence pairs and labels
    return pairs


def sentence_pairs_generation_multilabel(sentences, labels, pairs):
    # Initialize two empty lists to hold the (sentence, sentence) pairs and
    # labels to indicate if a pair is positive or negative
    for first_idx in range(len(sentences)):
        current_sentence = sentences[first_idx]
        sample_labels = np.where(labels[first_idx, :] == 1)[0]
        if len(np.where(labels.dot(labels[first_idx, :].T) == 0)[0]) == 0:
            continue
        else:
            for _label in sample_labels:
                second_idx = np.random.choice(np.where(labels[:, _label] == 1)[0])
                positive_sentence = sentences[second_idx]
                # Prepare a positive pair and update the sentences and labels
                # lists, respectively
                pairs.append(InputExample(texts=[current_sentence, positive_sentence], label=1.0))

            # Search for sample that don't have a label in common with current
            # sentence
            negative_idx = np.where(labels.dot(labels[first_idx, :].T) == 0)[0]
            negative_sentence = sentences[np.random.choice(negative_idx)]
            # Prepare a negative pair of sentences and update our lists
            pairs.append(InputExample(texts=[current_sentence, negative_sentence], label=0.0))
    # Return a 2-tuple of our sentence pairs and labels
    return pairs


def sentence_pairs_generation_cos_sim(sentences, pairs, cos_sim_matrix):
    # initialize two empty lists to hold the (sentence, sentence) pairs and
    # labels to indicate if a pair is positive or negative

    idx = list(range(len(sentences)))

    for first_idx in range(len(sentences)):
        current_sentence = sentences[first_idx]
        second_idx = int(np.random.choice([x for x in idx if x != first_idx]))

        cos_sim = float(cos_sim_matrix[first_idx][second_idx])
        paired_sentence = sentences[second_idx]
        pairs.append(InputExample(texts=[current_sentence, paired_sentence], label=cos_sim))

        third_idx = np.random.choice([x for x in idx if x != first_idx])
        cos_sim = float(cos_sim_matrix[first_idx][third_idx])
        paired_sentence = sentences[third_idx]
        pairs.append(InputExample(texts=[current_sentence, paired_sentence], label=cos_sim))

    return pairs

# Copyright 2022 MosaicML Composer authors
# SPDX-License-Identifier: Apache-2.0

"""A collection of common torchmetrics for NLP tasks."""

import ast
import logging
import os
import random
import re
import string
import warnings
from copy import deepcopy
from typing import Any, Callable, Dict, List, Mapping, Optional, Union

import numpy as np
import torch
from torch import Tensor
from torch.nn import functional as F
from torchmetrics import Metric

from composer.utils.eval_client import EvalClient, LambdaEvalClient, LocalEvalClient, MosaicMLLambdaEvalClient
from composer.utils.import_helpers import MissingConditionalImportError

log = logging.getLogger(__name__)

__all__ = [
    'InContextLearningLMAccuracy',
    'InContextLearningMultipleChoiceAccuracy',
    'InContextLearningQAAccuracy',
    'InContextLearningCodeEvalAccuracy',
    # 'InContextLearningLLMAsAJudge',
    'IFEvalJudge',
    'MTBenchJudge',
    'BinaryF1Score',
    'LanguageCrossEntropy',
    'MaskedAccuracy',
    'LanguagePerplexity',
    'InContextLearningLMExpectedCalibrationError',
    'InContextLearningMCExpectedCalibrationError',
]


class MaskedAccuracy(Metric):
    """Computes accuracy with support for masked indices.

    Adds metric state variables:
        correct (float): The number of instances where the prediction masked the target.
        total (float): The number of total instances that were predicted.

    Args:
        ignore_index (int): The class index to ignore. Default: -100.
        dist_sync_on_step (bool, optional): Synchronize metric state across processes at
            each forward() before returning the value at the step. Default: ``False``.
    """

    # Make torchmetrics call update only once
    full_state_update = False

    def __init__(self, ignore_index: int = -100, dist_sync_on_step: bool = False):
        # state from multiple processes
        super().__init__(dist_sync_on_step=dist_sync_on_step)
        self.ignore_index = ignore_index

        self.add_state('correct', default=torch.tensor(0), dist_reduce_fx='sum')
        self.add_state('total', default=torch.tensor(0), dist_reduce_fx='sum')

    def update(self, preds: torch.Tensor, target: torch.Tensor):
        # predictions is a batch x num_classes tensor, take the argmax to get class indices
        preds = torch.argmax(preds, dim=-1)
        assert preds.shape == target.shape

        # mask out the padded indices
        mask = (target != self.ignore_index)
        masked_target = target[mask]
        masked_preds = preds[mask]

        self.correct += torch.sum(masked_preds == masked_target)
        self.total += mask.sum()

    def compute(self):
        assert isinstance(self.correct, Tensor)
        assert isinstance(self.total, Tensor)
        return self.correct.float() / self.total


class LanguageCrossEntropy(Metric):
    """Torchmetric that computes cross entropy on language modeling outputs.

    Adds metric state variables:
        sum_loss (float): The sum of the per-example loss in the batch.
        total_items (float): The number of batches to average across.

    Args:
        dist_sync_on_step (bool, optional): Synchronize metric state across processes at
            each forward() before returning the value at the step. Default: ``False``.
        ignore_index (int, optional): The class index to ignore. Default: ``-100``.
    """

    # Make torchmetrics call update only once
    full_state_update = False

    def __init__(self, dist_sync_on_step: bool = False, ignore_index: int = -100):
        super().__init__(dist_sync_on_step=dist_sync_on_step)

        self.ignore_index = ignore_index
        self.loss_fn = torch.nn.CrossEntropyLoss(ignore_index=ignore_index, reduction='sum')
        self.add_state('sum_loss', default=torch.tensor(0.), dist_reduce_fx='sum')
        self.add_state('total_items', default=torch.tensor(0), dist_reduce_fx='sum')

    def update(self, output: Union[Mapping, Tensor], target: Tensor) -> None:
        """Updates the internal state with results from a new batch.

        Args:
            output (Mapping): The output from the model, which must contain
                either the Tensor or a Mapping type that contains the loss or model logits.
            target (~torch.Tensor): A Tensor of ground-truth values to compare against.
        """
        if isinstance(output, Mapping):
            logits = output['logits']
        elif isinstance(output, Tensor):
            logits = output
        else:
            raise Exception(f'Type {type(output)} for the output is unsupported.')

        target = target.view(-1)
        logits = logits.view(target.shape[0], -1)
        losses = self.loss_fn(logits, target)

        total_items = (target != self.ignore_index).sum()
        self.total_items += total_items  #type: ignore (third-party)

        # accumulate loss over all batches
        self.sum_loss += losses

    def compute(self) -> Tensor:
        """Aggregate the state over all processes to compute the metric.

        Returns:
            loss: The loss averaged across all batches as a :class:`~torch.Tensor`.
        """
        # Return average loss over entire dataset
        return self.sum_loss / self.total_items  #type: ignore (third-party)


class BinaryF1Score(Metric):
    """Implements F1 Scores for binary classification tasks via sklearn.

    Adds metric state variables:
        true_positive (float): A counter of how many items were correctly classified as positives.
        false_positive (float): A counter of how many items were incorrectly classified as positives.
        false_negative (float): A counter of how many items were incorrectly classified as negatives.

    Args:
        dist_sync_on_step (bool, optional): Synchronize metric state across processes at
            each forward() before returning the value at the step. Default: ``False``.
    """

    # Make torchmetrics call update only once
    full_state_update = False

    def __init__(self, dist_sync_on_step: bool = False):
        super().__init__(dist_sync_on_step=dist_sync_on_step)

        self.add_state('true_positive', default=torch.tensor(0.), dist_reduce_fx='sum')
        self.add_state('false_positive', default=torch.tensor(0), dist_reduce_fx='sum')
        self.add_state('false_negative', default=torch.tensor(0), dist_reduce_fx='sum')

    def update(self, output: Tensor, target: Tensor) -> None:
        """Updates the internal state with results from a new batch.

        Args:
            output (Mapping): The output from the model, which must contain
                either the Tensor or a Mapping type that contains the loss or model logits.
            target (~torch.Tensor): A Tensor of ground-truth values to compare against.
        """
        predictions = torch.argmax(output, dim=1)
        self.true_positive += predictions[(target == 1)].sum()
        self.false_positive += (predictions[(target == 1)] == 0).sum()
        self.false_negative += (predictions[(target == 0)] == 1).sum()

    def compute(self) -> Tensor:
        """Aggregate the state over all processes to compute the metric.

        Returns:
            loss: The loss averaged across all batches as a :class:`~torch.Tensor`.
        """
        assert isinstance(self.true_positive, Tensor)
        assert isinstance(self.false_positive, Tensor)
        assert isinstance(self.false_negative, Tensor)
        f1 = (self.true_positive) / (self.true_positive + (0.5 * (self.false_negative + self.false_positive)))
        return f1


class LanguagePerplexity(LanguageCrossEntropy):
    """Subclasses :class:`~composer.metrics.nlp.LanguageCrossEntropy` to implement perplexity."""

    def compute(self) -> Tensor:
        """Returns torch.exp() of the LanguageCrossEntropy."""
        avg_loss = super().compute()
        return torch.exp(avg_loss)


class InContextLearningMetric(Metric):

    def __init__(self, dist_sync_on_step=False, cache_responses=False):
        super().__init__(dist_sync_on_step=dist_sync_on_step)
        self.add_state('response_cache', default=[], dist_reduce_fx=None)
        self.cache_responses = cache_responses

    def reset_response_cache(self, cache: bool):
        self.cache_responses = cache
        setattr(self, 'response_cache', [])

    def format_response_cache(self, tokenizer):
        columns, rows = None, None
        assert isinstance(self.response_cache, list)
        if self.cache_responses and len(self.response_cache) > 0:
            rows = []
            for row in self.response_cache:
                assert isinstance(row, dict)
                columns = list(row.keys())
                converted_row = []
                for r_i in row.values():
                    if isinstance(r_i, list) and len(r_i) > 0 \
                        and all(isinstance(r_ij, int) for r_ij in r_i) \
                        and not all(isinstance(r_ij, bool) for r_ij in r_i):
                        # remove all padding tokens
                        r_i = [t for t in r_i if t not in tokenizer.all_special_ids]
                        converted_row.append(tokenizer.decode(r_i))
                    else:
                        converted_row.append(r_i)
                rows.append(converted_row)

        return columns, rows

    def update(self, batch: dict, output_logits: torch.Tensor, labels: torch.Tensor):
        """Abstract interface for computing an in-context learning metrics.

        Args:
            batch (dict): Batch must consist minimally of `input_ids` as well as any other structure needed
                to compute the metric.
            output_logits (torch.Tensor): The model outputs evaluated on the batch `input_ids`
            labels (torch.Tensor): The correct outputs.

        Raises:
            NotImplementedError: Abstract method must be implemented by subclasses
        """
        raise NotImplementedError

    def sync(
        self,
        dist_sync_fn: Optional[Callable] = None,
        process_group: Optional[Any] = None,
        should_sync: bool = True,
        distributed_available: Optional[Callable] = None,
    ):
        # this is based off the gather_all_tensors utility function in torchmetrics, except it works with non-tensor objects
        # (in particular, lists of strings). Link here: https://github.com/Lightning-AI/torchmetrics/blob/99d6d9d6ac4eb1b3398241df558604e70521e6b0/src/torchmetrics/utilities/distributed.py#L97-L148
        if should_sync:
            group = process_group or self.process_group
            world_size = torch.distributed.get_world_size(group)  # pyright: ignore [reportGeneralTypeIssues]
            torch.distributed.barrier(group=group)  # pyright: ignore [reportGeneralTypeIssues]
            gathered_response_cache = [[]] * world_size
            torch.distributed.all_gather_object(  # pyright: ignore [reportGeneralTypeIssues]
                gathered_response_cache, self.response_cache)
            flattened_gathered_response_cache = [item for row in gathered_response_cache for item in row]
            setattr(self, 'response_cache', flattened_gathered_response_cache)
            super().sync(
                dist_sync_fn,
                process_group,
                should_sync,
                distributed_available,
            )


class InContextLearningQAAccuracy(InContextLearningMetric):
    r"""Computes accuracy for In-context learning (ICL) question answering (QA) tasks.

    ICL QA tasks consist of some number of example question answering tasks (referred to as the 'context'), followed by a test task where the model must
    match one of the possible answer aliases (referred to as the 'continuation').

    For example, the model may be provided the context below and evaluated on its ability to correctly predict the continuation.

    Context: `Question: Who was president of the United States in 2012?\nAnswer: Barack Obama\nQuestion: Is water wet?\nAnswer: `
    Continuation: [`yes`, `no`]

    Both predictions and answers will be normalized before comparison.

    Adds metric state variables:
        correct (float): The number of instances where the prediction was a prefix for any of the answer aliases.
        total (float): The number of total instances that were predicted.

    Args:
        dist_sync_on_step (bool, optional): Synchronize metric state across processes at
            each forward() before returning the value at the step. Default: ``False``.
    """

    # Make torchmetrics call update only once
    full_state_update = False

    def __init__(self, dist_sync_on_step: bool = False, cache_responses: bool = False):
        # state from multiple processes
        super().__init__(dist_sync_on_step=dist_sync_on_step, cache_responses=cache_responses)
        self.add_state('correct', default=torch.tensor(0.), dist_reduce_fx='sum')
        self.add_state('total', default=torch.tensor(0.), dist_reduce_fx='sum')

    def normalize_answer(self, answer: str):
        """Lower text and remove punctuation, articles and extra whitespace.

        Copied from https://github.com/mandarjoshi90/triviaqa/blob/master/evaluation/triviaqa_evaluation.py
        """

        def remove_articles(text: str) -> str:
            return re.sub(r'\b(a|an|the)\b', ' ', text)

        def white_space_fix(text: str) -> str:
            return ' '.join(text.split())

        def handle_punc(text: str) -> str:
            exclude = set(string.punctuation + ''.join([u'‘', u'’', u'´', u'`']))
            return ''.join(ch if ch not in exclude else ' ' for ch in text)

        def lower(text: str) -> str:
            return text.lower()

        def replace_underscore(text: str) -> str:
            return text.replace('_', ' ')

        return white_space_fix(remove_articles(handle_punc(lower(replace_underscore(answer))))).strip()

    def update(self, outputs: List[str], labels: List[List[str]], batch: Optional[Dict[str, Any]] = None):
        if batch is None:
            batch = {}
        cot_delimiter = batch.get('cot_delimiter', '')
        do_normalization = batch.get('do_normalization', True)
        stopping_criteria = batch.get('stopping_criteria', None)
        for sample_output, sample_labels, prompt_tensor in zip(outputs, labels, batch['input_ids']):

            final_answer = sample_output
            if stopping_criteria is not None and len(stopping_criteria) > 0:
                final_answer = re.split('|'.join(stopping_criteria), final_answer)[0]

            if cot_delimiter is not None and len(cot_delimiter) > 0:
                final_answer = final_answer.split(cot_delimiter)[-1]

            if do_normalization:
                cleaned_final_answer = self.normalize_answer(final_answer)
                cleaned_sample_labels = {self.normalize_answer(label) for label in sample_labels}
            else:
                cleaned_final_answer = final_answer
                cleaned_sample_labels = set(sample_labels)

            correct = False
            if any(cleaned_final_answer.startswith(label) for label in cleaned_sample_labels):
                self.correct += torch.tensor(1.0)
                correct = True

            assert isinstance(self.response_cache, list)
            self.response_cache.append({
                'prompt': prompt_tensor.tolist(),
                'original_model_output': sample_output,
                'cleaned_model_output': cleaned_final_answer,
                'original_labels': sample_labels,
                'cleaned_labels': cleaned_sample_labels,
                'correct': correct
            })
            self.total += torch.tensor(1.0)

    def compute(self):
        super().compute()
        assert isinstance(self.correct, Tensor)
        assert isinstance(self.total, Tensor)
        return self.correct / self.total


class InContextLearningLLMAsAJudge(InContextLearningMetric):
    r"""Computes accuracy for In-context learning (ICL) question answering (QA) tasks.

    ICL QA tasks consist of some number of example question answering tasks (referred to as the 'context'), followed by a test task where the model must
    match one of the possible answer aliases (referred to as the 'continuation').

    For example, the model may be provided the context below and evaluated on its ability to correctly predict the continuation.

    Context: `Question: Who was president of the United States in 2012?\nAnswer: Barack Obama\nQuestion: Is water wet?\nAnswer: `
    Continuation: [`yes`, `no`]

    Both predictions and answers will be normalized before comparison.

    Adds metric state variables:
        correct (float): The number of instances where the prediction was a prefix for any of the answer aliases.
        total (float): The number of total instances that were predicted.

    Args:
        dist_sync_on_step (bool, optional): Synchronize metric state across processes at
            each forward() before returning the value at the step. Default: ``False``.
    """

    # Make torchmetrics call update only once
    full_state_update = False
    # Respond with either "Yes" or "No" if you are able to make a distinction, or "Invalid" if the statements are malformatted.
    # Any response other than one "Yes", "No", or "Invalid" is unusable and will not be scored, so please adhere to the instructions carefully.

    BASE_EQUIVALENCE_PROMPT = """Please determine whether the supplied statements or answers are equivalent.
If one statment has a long continuation, only consider the first segment of the statement.
Respond with either "Yes" or "No". Any response other than one "Yes" or "No" is unusable and will not be scored, so please adhere to the instructions carefully.
Here are some examples to help you understand the task. They are not a part of the statements we are comparing.

Statement 1: The sky is blue.
Statement 2: The sky is blue.
Result: Yes

Statement 1: Computer hard drive
Statement 2: Solid state drive
Result: No

Statement 1: Potatos are nutritious.
Statement 2: Taters have many healthy benefits.
Result: Yes

Statement 1: Pytorch
Statement 2: no.
Result: No

Statement 1: The American team was the first to win the World Championship.
Statement 2: America
Result: Yes

Statement 1:  Yes\nQuestion: What is the name of the British Army_s first major infantry regiment?\nAnswer: The
Statement 2: Yes
Result: Yes

Statement 1:  Dik-dik\nQuestion: What type of animal is a kik-kik?\nAnswer: D
Statement 2: Antelope
Result: No

The statements follow:
"""
    BASE_USER_INPOUT = """Statement 1: {statement1}
Statement 2: {statement2}
Result: """

    def __init__(self, dist_sync_on_step: bool = False, tokenizer: Optional[Any] = None, prompt: Optional[str] = None):
        # state from multiple processes
        super().__init__(dist_sync_on_step=dist_sync_on_step)
        self.add_state('correct', default=torch.tensor(0.), dist_reduce_fx='sum')
        self.add_state('invalid_judge_response', default=torch.tensor(0.), dist_reduce_fx='sum')
        self.add_state('total', default=torch.tensor(0.), dist_reduce_fx='sum')
        # TODO: allow different models
        # self.init_openai()
        self.client = None

    def init_openai(self):
        try:
            from openai import OpenAI
        except ImportError as e:
            raise MissingConditionalImportError(extra_deps_group='openai',
                                                conda_package='openai',
                                                conda_channel='conda-forge') from e
        self.client = OpenAI()

    def call_judge(self, sample_answer, sample_label) -> List[str]:
        # TODO: allow different models
        openai_user_input = deepcopy(self.BASE_USER_INPOUT)
        if sample_answer.startswith(' '):
            sample_answer = sample_answer.lstrip()

        # Randomly choose the true answer or the model output to be the first statment
        # to avoid some model bias
        if random.random() <= .5:
            formatted_input = openai_user_input.format(statement1=sample_answer, statement2=sample_label)
        else:
            formatted_input = openai_user_input.format(statement1=sample_label, statement2=sample_answer)
        response = self.client.chat.completions.create(
            # TODO: allow configurations
            model='gpt-3.5-turbo',
            messages=[{
                'role': 'system',
                'content': self.BASE_EQUIVALENCE_PROMPT
            }, {
                'role': 'user',
                'content': formatted_input
            }],
            max_tokens=10)
        if 'Yes' not in response.choices[0].message.content and 'No' not in response.choices[0].message.content:
            print('Found an illformatted response:')
            print(formatted_input + response.choices[0].message.content)

        return response.choices[0].message.content

    def update(self, batch: Dict[str, Any], outputs: List[str], labels: List[List[str]]):
        if not self.client:
            self.init_openai()
        for sample_output, sample_answer in zip(outputs, batch['answer']):
            sample_output = sample_output.split('\n')[0]
            result = self.call_judge(sample_output, sample_answer)
            if result.endswith('Yes'):
                self.correct += torch.tensor(1.0)
            elif result.endswith('No'):
                pass
            else:
                self.invalid_judge_response += torch.tensor(1.0)
            self.total += torch.tensor(1.0)

        # OpenAI Client can't be copied by deepcopy and will throw an error, so we delete it after we use it
        # Initializatin takes ~12 ms
        del self.client
        self.client = None

    def compute(self):
        print('correct:', self.correct)
        print('total:', self.total)
        print('invalid:', self.invalid_judge_response)
        assert isinstance(self.correct, Tensor)
        assert isinstance(self.total, Tensor)
        return self.correct / self.total


class InContextLearningLMAccuracy(InContextLearningMetric):
    r"""Computes accuracy for In-context learning (ICL) language modeling (LM) tasks.

    ICL LM tasks consist of some number of example language modeling tasks (referred to as the 'context'), followed by a test task where the model must correctly predict all the tokens
    following tokens in some passage (referred to as the 'continuation').

    For example, the model may be provided the context below and evaluated on its ability to correctly predict the continuation. Note: it doesn't matter
    whether the model correctly predicts the context tokens.

    Context: `The dog is->fuzzy\nthe water is->hot\nthe tree is->`
    Continuation: `green`

    Adds metric state variables:
        correct (float): The number of instances where the prediction masked the target.
        total (float): The number of total instances that were predicted.

    Args:
        dist_sync_on_step (bool, optional): Synchronize metric state across processes at
            each forward() before returning the value at the step. Default: ``False``.
    """

    # Make torchmetrics call update only once
    full_state_update = False

    def __init__(self, dist_sync_on_step: bool = False, cache_responses: bool = False):
        # state from multiple processes
        super().__init__(dist_sync_on_step=dist_sync_on_step, cache_responses=cache_responses)
        self.add_state('correct', default=torch.tensor(0.), dist_reduce_fx='sum')
        self.add_state('total', default=torch.tensor(0.), dist_reduce_fx='sum')

    def update(self, batch: dict, output_logits: torch.Tensor, labels: torch.Tensor):
        for batch_idx, cont_idx in enumerate(batch['continuation_indices']):
            cont_tok_pred = output_logits[batch_idx].index_select(dim=0, index=cont_idx - 1).argmax(dim=-1)
            cont_tok_targ = labels[batch_idx].index_select(dim=0, index=cont_idx - 1)

            correct = False
            if (cont_tok_pred == cont_tok_targ).all().int() == 1:
                self.correct += torch.tensor(1.0)
                correct = True
            if self.cache_responses:
                assert isinstance(self.response_cache, list)
                self.response_cache.append({
                    'context_tok': batch['input_ids'][batch_idx][:cont_idx[0]].tolist(),
                    'continuation_tok_target': cont_tok_targ.tolist(),
                    'continuation_tok_pred': cont_tok_pred.tolist(),
                    'correct': correct
                })
            self.total += torch.tensor(1.0)

    def compute(self):
        super().compute()
        assert isinstance(self.correct, Tensor)
        assert isinstance(self.total, Tensor)
        return self.correct / self.total


class InContextLearningMultipleChoiceAccuracy(InContextLearningMetric):
    r"""Computes accuracy for In-context learning (ICL) multiple choice (MC) tasks.

    ICL MC tasks consists of a series of questions with some number of possible choices (only one of which can be correct).
    At inference time each possible choice is given to the model as a separate input and the one for which the model assigns
    the lowest perplexity to the choice is considered the model's choice. The model is correct if it "chooses" the right answer.

    Context: `The dog is->fuzzy\nthe water is->hot\nthe tree is->`
    Continuation: `green`

    Adds metric state variables:
        correct (float): The number of instances where the prediction masked the target.
        total (float): The number of total instances that were predicted.

    Args:
        dist_sync_on_step (bool, optional): Synchronize metric state across processes at
            each forward() before returning the value at the step. Default: ``False``.
    """

    # Make torchmetrics call update only once
    full_state_update = False

    def __init__(self, dist_sync_on_step: bool = False, cache_responses: bool = False):
        # state from multiple processes
        super().__init__(dist_sync_on_step=dist_sync_on_step, cache_responses=cache_responses)
        self.add_state('correct', default=torch.tensor(0.0), dist_reduce_fx='sum')
        self.add_state('total', default=torch.tensor(0.0), dist_reduce_fx='sum')

    def update(self, batch: dict, output_logits: torch.Tensor, labels: torch.Tensor):
        perplexities = []
        for batch_idx, cont_idx in enumerate(batch['continuation_indices']):
            # continuation indices refer to indices in the original input's token space
            cont_tok_logits = output_logits[batch_idx].index_select(dim=0, index=cont_idx - 1)
            # labels have been shifted left by one index, so the cont_idx needs to be shifted as well.
            cont_tok_targ = labels[batch_idx].index_select(dim=0, index=cont_idx - 1)
            cross_entropy = F.cross_entropy(cont_tok_logits, cont_tok_targ)
            perplexity = torch.exp(cross_entropy)
            perplexities.append(perplexity)

        for (start, end), gold_idx in zip(batch['choice_groupings'], batch['gold_indices']):
            subset = perplexities[start:end]
            idx_min = subset.index(min(subset))
            correct = False
            if idx_min == gold_idx:
                self.correct += torch.tensor(1.0)
                correct = True

            if self.cache_responses:
                question = batch['input_ids'][start][:batch['continuation_indices'][start][0]]
                correct_choice = batch['input_ids'][start:end][gold_idx][batch['continuation_indices'][start:end][
                    gold_idx][0]:batch['continuation_indices'][start:end][gold_idx][-1] + 1]
                selected_choice = batch['input_ids'][start:end][idx_min][batch['continuation_indices'][start:end][
                    idx_min][0]:batch['continuation_indices'][start:end][idx_min][-1] + 1]

                assert isinstance(self.response_cache, list)
                self.response_cache.append({
                    'question_tok': question.tolist(),
                    'correct_choice': correct_choice.tolist(),
                    'selected_choice': selected_choice.tolist(),
                    'correct': correct
                })
            self.total += torch.tensor(1.0)

    def compute(self):
        super().compute()
        assert isinstance(self.correct, Tensor)
        assert isinstance(self.total, Tensor)
        return self.correct.float() / self.total


class InContextLearningExpectedCalibrationError(InContextLearningMetric):
    """Generic class for Expected Calibration Error (ECE) (cite: https://arxiv.org/pdf/1706.04599.pdf).

    Expected calibration error is calculated by dividing predictions into buckets based on the model's confidence (a probability value between 0 and 1).
    We then calculate the accuracy within each bucket and calculate the average gap between confidence and accuracy
    across buckets, weighted by the number of samples in each bucket.

    Each task must implement its own definition of "confidence" to be computed via the `update` method.

    Adds metric state variables:
    bucket_totals (float): The number of instances where the prediction masked the target per bucket.
    bucket_correct (float): The number of total instances that were predicted per bucket.

    Args:
        dist_sync_on_step (bool, optional): Synchronize metric state across processes at
            each forward() before returning the value at the step. Default: ``False``.
        n_buckets (int): Number of distinct buckets to split the confidence distribution into
    """

    def __init__(self, dist_sync_on_step: bool = False, n_buckets: int = 10):
        # state from multiple processes
        super().__init__(dist_sync_on_step=dist_sync_on_step)
        self.n_buckets = n_buckets
        if n_buckets < 1:
            raise Exception('`n_buckets`')
        self.add_state('bucket_totals', default=torch.zeros(n_buckets), dist_reduce_fx='sum')
        self.add_state('bucket_correct', default=torch.zeros(n_buckets), dist_reduce_fx='sum')

    def update(self, batch: dict, output_logits: torch.Tensor, labels: torch.Tensor):
        pass

    def compute(self):
        assert isinstance(self.bucket_correct, Tensor)
        assert isinstance(self.bucket_totals, Tensor)

        result = torch.tensor(0.0, device=self.bucket_correct.device)
        total_obs = torch.sum(self.bucket_totals)
        for i in range(self.n_buckets):
            if self.bucket_totals[i] == 0:
                continue

            acc_bucket_i = self.bucket_correct[i] / self.bucket_totals[i]
            upper_bound = (i + 1) / self.n_buckets
            lower_bound = i / self.n_buckets
            conf_bucket_i = torch.tensor((upper_bound + lower_bound) / 2, device=self.bucket_correct.device)
            result += (self.bucket_totals[i] / total_obs) * torch.abs(acc_bucket_i - conf_bucket_i)
        return result


class InContextLearningMCExpectedCalibrationError(InContextLearningExpectedCalibrationError):
    r"""Computes Expected Calibration Error (ECE) for In-context learning (ICL) multiple choice (MC) tasks. (source: https://arxiv.org/abs/2012.00955).

    For MC tasks, the model confidence is defined as the softmax of average per-token probability assigned to the top question choice.

    See `InContextLearningExpectedCalibrationError` for more info.
    """

    # Make torchmetrics call update only once
    full_state_update = False

    def update(self, batch: Dict[str, Any], output_logits: torch.Tensor, labels: torch.Tensor):
        output_logits = torch.softmax(output_logits, dim=2)
        probabilites = []
        for batch_idx, cont_idx in enumerate(batch['continuation_indices']):
            cont_tok_logits = output_logits[batch_idx].index_select(dim=0, index=cont_idx - 1)
            cont_tok_targ = labels[batch_idx].index_select(dim=0, index=cont_idx - 1)
            probability = cont_tok_logits.index_select(dim=1, index=cont_tok_targ).diagonal().mean()
            probabilites.append(probability)

        for (start, end), gold_idx in zip(batch['choice_groupings'], batch['gold_indices']):
            subset = probabilites[start:end]
            idx_max = subset.index(max(subset))
            confidence = torch.tensor(subset).max() / torch.tensor(subset).sum()

            assert confidence >= 0.0 and confidence <= 1.0
            bucket_idx = int(confidence * self.n_buckets)
            if bucket_idx == self.n_buckets:
                bucket_idx -= 1

            if idx_max == gold_idx:
                self.bucket_correct[bucket_idx] += 1  # pyright: ignore [reportGeneralTypeIssues]

            self.bucket_totals[bucket_idx] += 1  # pyright: ignore [reportGeneralTypeIssues]


class InContextLearningLMExpectedCalibrationError(InContextLearningExpectedCalibrationError):
    r"""Computes Expected Calibration Error (ECE) for In-context learning (ICL) language modeling (LM) tasks. (cite: https://arxiv.org/pdf/1706.04599.pdf).

    For LM tasks, the model confidence is defined as the minimum probability assigned to all tokens in the continuation.

    See `InContextLearningExpectedCalibrationError` for more info.
    """

    # Make torchmetrics call update only once
    full_state_update = False

    def update(self, batch: Dict[str, Any], output_logits: torch.Tensor, labels: torch.Tensor):
        output_logits = torch.softmax(output_logits, dim=2)
        for batch_idx, cont_idx in enumerate(batch['continuation_indices']):
            cont_tok_logits = output_logits[batch_idx].index_select(dim=0, index=cont_idx - 1)
            cont_tok_pred = cont_tok_logits.argmax(dim=-1)
            confidence = cont_tok_logits.max(dim=-1).values.min()
            cont_tok_targ = labels[batch_idx].index_select(dim=0, index=cont_idx - 1)
            assert confidence >= 0.0 and confidence <= 1.0
            bucket_idx = int(confidence * self.n_buckets)
            if bucket_idx == self.n_buckets:
                bucket_idx -= 1

            if (cont_tok_pred == cont_tok_targ).all():
                self.bucket_correct[bucket_idx] += 1  # pyright: ignore [reportGeneralTypeIssues]

            self.bucket_totals[bucket_idx] += 1  # pyright: ignore [reportGeneralTypeIssues]


class InContextLearningCodeEvalAccuracy(InContextLearningMetric):
    r"""Computes accuracy for In-context learning (ICL) code evaluation tasks.

    ICL code eval tasks consist of some number of example code eval tasks (referred to as the 'context'), followed by a test task where the model must
    complete the code, where we term the code completion a 'continuation'.

    In each case, the model constructs a given number of continuations (termed pass@K for K continuations), and each continuation is run against a set of test cases. The model is considered
    correct if at least one of the proposed continuations passes all the test cases.

    Runs on AWS Lambdas by default.

    Adds metric state variables:
        correct (float): The number of instances where the predictions passed all the test cases.
        total (float): The number of total instances that were predicted.

    Args:
        dist_sync_on_step (bool, optional): Synchronize metric state across processes at
            each forward() before returning the value at the step. Default: ``False``.
    """

    # Make torchmetrics call update only once
    full_state_update = False

    def __init__(self, dist_sync_on_step: bool = False, cache_responses: bool = False):
        # state from multiple processes
        super().__init__(dist_sync_on_step=dist_sync_on_step, cache_responses=cache_responses)
        self.add_state('correct', default=torch.tensor(0.), dist_reduce_fx='sum')
        self.add_state('total', default=torch.tensor(0.), dist_reduce_fx='sum')

        self.eval_device = os.environ.get('CODE_EVAL_DEVICE', None)
        if self.eval_device is not None:
            self.eval_device = self.eval_device.upper()

    def get_client(self) -> EvalClient:
        """Returns a client for the appropriate remote platform."""
        client = None
        if self.eval_device == 'LOCAL':
            warnings.warn(
                'Running code eval locally may be insecure. Please set environment variable CODE_EVAL_DEVICE '
                'to LAMBDA to run on remote. To use Lambdas, spin up your instance that checks code, set the URL as '
                'CODE_EVAL_URL and the API key as CODE_EVAL_APIKEY.')
            log.debug('Running code eval locally.')
            client = LocalEvalClient()
        elif self.eval_device == 'LAMBDA':
            client = LambdaEvalClient()
        elif self.eval_device == 'MOSAICML':
            client = MosaicMLLambdaEvalClient()
        elif self.eval_device is None:
            raise ValueError(
                'Attempting to use InContextLearningCodeEvalAccuracy but environment '
                'variable `CODE_EVAL_DEVICE` is not set. Please set it to `CODE_EVAL_DEVICE` '
                'to one of `LOCAL` (for unsafe local eval), `LAMBDA` (for AWS lambda ',
                'evaluation), or `MOSAICML` (for lambda eval through MAPI).')
        else:
            raise ValueError('Environment variable `CODE_EVAL_DEVICE` must be one of `LOCAL`, '
                             f'`LAMBDA`, or `MOSAICML` but got {self.eval_device}.')

        return client

    def estimator(self, n: int, c: int, k: int) -> float:
        """Computes the pass@k metric.

        Given the number of generated samples, n, the number of correct samples, c, and the k of interest,
        this function calculates pass@k as 1 - comb(n - c, k) / comb(n, k) as per the definition of
        pass@k in the HumanEval paper (https://arxiv.org/abs/2107.03374) and it's associated implementation:
        https://github.com/openai/human-eval.
        """
        if n - c < k:
            return 1.0
        return 1.0 - float(np.prod(1.0 - k / np.arange(n - c + 1, n + 1)))

    def update(self, batch: Dict[str, Any], outputs: List[str], labels: List[str]):
        """Updates the pass@k accuracy of code generation.

        Given a batch of prompts, test cases, and code generations, evaluates the code generations
        against the test cases and augments the pass@k accuracy of the batch to the values so far.

        Args:
            batch (Dict[str, Any]): A batch of data produced by the InContextLearningCodeEvalDataset, with
            the prompt, test cases, and entry points. This will be a dictionary that must have the following
            arguments:
            {
                'prompts': List[str],
                'test_inputs': List[List[str]],
                'test_outputs': List[List[str]],
                'entry_points': List[str],
                'languages': List[str],
                'generation_kwargs': Dict[str, Any]
            }
            outputs (List[str]): A list of code generations in the format of HF generate with beam search,
            which is the a list of strings in groups of beam_size e.g. for beam size 2 and batch size 2, the list
            will be of the format [prompt 1 gen 1, prompt 1 gen 2, prompt 2 gen 1, prompt 2 gen 2]
            labels (List[str]): A list of the correct code generations, for compatibility with existing HF generate
            functionalities. This is not used.
        """
        del labels  # never used
        client = self.get_client()

        pass_at_k = batch['pass_at_k']
        num_generations = batch['generation_kwargs']['num_return_sequences']
        processed_outputs = [
            outputs[i * num_generations:(i + 1) * num_generations] for i in range(len(batch['prompts']))
        ]
        payloads = []
        for sample_outputs, sample_prompt, test_inputs, test_outputs, entry_point, language in zip(
                processed_outputs, batch['prompts'], batch['test_inputs'], batch['test_outputs'], batch['entry_points'],
                batch['languages']):
            self.total += torch.tensor(1.0)
            prompt_payload = []
            for code_gen in sample_outputs:
                code_gen = re.split(r'\n[A-Za-z0-9#`]', code_gen)[0]  # remove everything after function ends
                final_code = sample_prompt + code_gen  # combine prompt with the code generation
                generation_payload = []
                for test_input, test_output in zip(test_inputs, test_outputs):
                    payload = {
                        'code': final_code,
                        'input': test_input,
                        'output': test_output,
                        'entry_point': entry_point,
                        'language': language,
                    }
                    generation_payload.append(payload)

                prompt_payload.append(generation_payload)
            payloads.append(prompt_payload)

        results = client.invoke(payloads)

        for test_result, code_gen_payload, in zip(results, payloads):
            num_correct = 0
            all_tests_passed = []
            for generation in test_result:
                correct = all(generation)
                all_tests_passed.append(correct)
                if correct:
                    num_correct += 1

            pass_at_k_rate = self.estimator(num_generations, num_correct, pass_at_k)
            self.correct += torch.tensor(pass_at_k_rate)

            if self.cache_responses:
                code_completions = [c[0]['code'] for c in code_gen_payload]
                assert isinstance(self.response_cache, list)
                self.response_cache.append({
                    'code_completions': code_completions,
                    'all_tests_passed': all_tests_passed,
                    'pass_at_k_rate': pass_at_k_rate
                })

        client.close()  # pyright: ignore [reportOptionalMemberAccess]

    def compute(self):
        super().compute()
        assert isinstance(self.correct, Tensor)
        assert isinstance(self.total, Tensor)
        return self.correct / self.total


class IFEvalJudge(InContextLearningMetric):
    """

    {
        "key": 3757,
        "prompt": "Would you consider yourself to be smart? Choose from:\nMy answer is yes.\nMy answer is no.\nMy answer is maybe.\nJust choose one phrase from above as your answer.",
        "instruction_id_list": ["detectable_format:constrained_response"],
        "kwargs": [{}]
    }
    {
    'key': 1001,
    'instruction_id_list': ['punctuation:no_comma'],
    'prompt': 'I am planning a trip to Japan, and I would like thee to write an '
            'itinerary for my journey in a Shakespearean style. You are not '
            'allowed to use any commas in your response.',
    'kwargs': [{}],
    'response': '<MODEL RESPONSE>'
    }

    """

    # Make torchmetrics call update only once
    full_state_update = False

    def __init__(self, dist_sync_on_step: bool = False, cache_responses: bool = False):
        super().__init__(dist_sync_on_step=dist_sync_on_step, cache_responses=cache_responses)

        self.add_state('prompt_total', default=torch.tensor(0.), dist_reduce_fx='sum')
        self.add_state('prompt_correct', default=torch.tensor(0.), dist_reduce_fx='sum')
        self.add_state('instruction_total', default=torch.tensor(0.), dist_reduce_fx='sum')
        self.add_state('instruction_correct', default=torch.tensor(0.), dist_reduce_fx='sum')

    def update(self, batch, outputs: Union[Mapping, Tensor], target: Tensor) -> None:
        """Updates the internal state with results from a new batch.

        """
        try:
            from instruction_following_eval import \
                instruction_following_eval  # pyright: ignore [reportGeneralTypeIssues]
            from instruction_following_eval.evaluation import InstructionResult
        except ImportError as e:
            raise MissingConditionalImportError(
                extra_deps_group='nlp',
                conda_package='datasets',
                conda_channel='conda-forge',
            ) from e
        batch_results = []
        for i, output in enumerate(outputs):
            kwargs = batch['kwargs'][i]
            # Removes k, v pairs when value is none for each dict in the the list
            kwargs = [{k: v for k, v in kwarg_dict.items() if v is not None} for kwarg_dict in kwargs]
            log.info('---------------------------------------')
            log.info(batch['prompt'][i])
            log.info('---------------------------------------')
            log.info(output)
            res = InstructionResult(key=batch['key'][i],
                                    instruction_id_list=batch['instruction_id_list'][i],
                                    prompt=batch['prompt'][i],
                                    kwargs=kwargs,
                                    response=output)
            result = instruction_following_eval([res], aggregate=False)
            log.info(result)
            # TODO: these dicts just get cast to strings
            if self.cache_responses:
                self.response_cache.append({
                    'key': batch['key'][i],
                    'instruction_id_list': batch['instruction_id_list'][i],
                    'prompt': batch['prompt'][i],
                    'kwargs': str(kwargs),
                    'response': output,
                    'result': str(result)
                })
            batch_results.append(result)
        for result in batch_results:
            self.prompt_total += 1
            if all(instruction['follow'] for instruction in result):
                self.prompt_correct += 1
            for instruction in result:
                self.instruction_total += 1
                if instruction['follow']:
                    self.instruction_correct += 1

    def compute(self) -> Tensor:
        """Aggregate the state over all processes to compute the metric.

        Returns:
            loss: The loss averaged across all batches as a :class:`~torch.Tensor`.
        """
        super().compute()
        prompt_acc = self.prompt_correct.float() / self.prompt_total
        instruction_acc = self.instruction_correct.float() / self.instruction_total
        log.debug(f'prompt_acc: {prompt_acc}')
        log.debug(f'promp_total: {self.prompt_total}')
        log.debug(f'instruct_acc: {instruction_acc}')
        log.debug(f'instruct_total: {self.instruction_total}')
        return instruction_acc


class MTBenchJudge(InContextLearningMetric):
    # Make torchmetrics call update only once
    full_state_update = False

    SINGLE_V1_SYSTEM_PROMPT = 'You are a helpful assistant.'
    SINGLE_V1 = "[Instruction]\nPlease act as an impartial judge and evaluate the quality of the response provided by an AI assistant to the user question displayed below. Your evaluation should consider factors such as the helpfulness, relevance, accuracy, depth, creativity, and level of detail of the response. Begin your evaluation by providing a short explanation. Be as objective as possible. After providing your explanation, you must rate the response on a scale of 1 to 10 by strictly following this format: \"[[rating]]\", for example: \"Rating: [[5]]\".\n\n[Question]\n{question}\n\n[The Start of Assistant's Answer]\n{answer}\n[The End of Assistant's Answer]"
    SINGLE_V1_MATH = "[Instruction]\nPlease act as an impartial judge and evaluate the quality of the response provided by an AI assistant to the user question displayed below. Your evaluation should consider correctness and helpfulness. You will be given a reference answer and the assistant's answer. Begin your evaluation by comparing the assistant's answer with the reference answer. Identify and correct any mistakes. Be as objective as possible. After providing your explanation, you must rate the response on a scale of 1 to 10 by strictly following this format: \"[[rating]]\", for example: \"Rating: [[5]]\".\n\n[Question]\n{question}\n\n[The Start of Reference Answer]\n{ref_answer_1}\n[The End of Reference Answer]\n\n[The Start of Assistant's Answer]\n{answer}\n[The End of Assistant's Answer]"

    MULTI_TURN_SYSTEM_PROMPT = "Please act as an impartial judge and evaluate the quality of the response provided by an AI assistant to the user question displayed below. Your evaluation should consider factors such as the helpfulness, relevance, accuracy, depth, creativity, and level of detail of the response. You evaluation should focus on the assistant's answer to the second user question. Begin your evaluation by providing a short explanation. Be as objective as possible. After providing your explanation, you must rate the response on a scale of 1 to 10 by strictly following this format: \"[[rating]]\", for example: \"Rating: [[5]]\".\n\n"
    SINGLE_V1_MULTI_TURN_TEMPLATE = "<|The Start of Assistant A's Conversation with User|>\n\n### User:\n{question_1}\n\n### Assistant A:\n{answer_1}\n\n### User:\n{question_2}\n\n### Assistant A:\n{answer_2}\n\n<|The End of Assistant A's Conversation with User|>"
    SINGLE_V1_MATH_MULTI_TURN_TEMPLATE_SYSTEM_PROMPT = "Please act as an impartial judge and evaluate the quality of the response provided by an AI assistant to the user question. Your evaluation should consider correctness and helpfulness. You will be given a reference answer and the assistant's answer. You evaluation should focus on the assistant's answer to the second question. Begin your evaluation by comparing the assistant's answer with the reference answer. Identify and correct any mistakes. Be as objective as possible. After providing your explanation, you must rate the response on a scale of 1 to 10 by strictly following this format: \"[[rating]]\", for example: \"Rating: [[5]]\".\n\n"
    SINGLE_V1_MATH_MULTI_TURN_TEMPLATE = "<|The Start of Reference Answer|>\n\n### User:\n{question_1}\n\n### Reference answer:\n{ref_answer_1}\n\n### User:\n{question_2}\n\n### Reference answer:\n{ref_answer_2}\n\n<|The End of Reference Answer|>\n\n\n<|The Start of Assistant A's Conversation with User|>\n\n### User:\n{question_1}\n\n### Assistant A:\n{answer_1}\n\n### User:\n{question_2}\n\n### Assistant A:\n{answer_2}\n\n<|The End of Assistant A's Conversation with User|>"

    ONE_SCORE_PATTERN = re.compile('\[\[(\d+\.?\d*)\]\]')
    ONE_SCORE_PATTERN_BACKUP = re.compile('\[(\d+\.?\d*)\]')

    def __init__(self, dist_sync_on_step: bool = False, cache_responses: bool = False):
        # state from multiple processes
        super().__init__(dist_sync_on_step=dist_sync_on_step, cache_responses=cache_responses)
        self.add_state('invalid_judge_response', default=torch.tensor(0.), dist_reduce_fx='sum')
        self.add_state('all_scores', default=torch.tensor(0.), dist_reduce_fx='sum')
        self.add_state('total', default=torch.tensor(0.), dist_reduce_fx='sum')

        self.add_state('math_score', default=torch.tensor(0.), dist_reduce_fx='sum')
        self.add_state('math_total', default=torch.tensor(0.), dist_reduce_fx='sum')
        self.add_state('reasoning_score', default=torch.tensor(0.), dist_reduce_fx='sum')
        self.add_state('reasoning_total', default=torch.tensor(0.), dist_reduce_fx='sum')
        self.add_state('stem_score', default=torch.tensor(0.), dist_reduce_fx='sum')
        self.add_state('stem_total', default=torch.tensor(0.), dist_reduce_fx='sum')
        self.add_state('humanities_score', default=torch.tensor(0.), dist_reduce_fx='sum')
        self.add_state('humanities_total', default=torch.tensor(0.), dist_reduce_fx='sum')
        self.add_state('extraction_score', default=torch.tensor(0.), dist_reduce_fx='sum')
        self.add_state('extraction_total', default=torch.tensor(0.), dist_reduce_fx='sum')
        self.add_state('coding_score', default=torch.tensor(0.), dist_reduce_fx='sum')
        self.add_state('coding_total', default=torch.tensor(0.), dist_reduce_fx='sum')
        self.add_state('roleplay_score', default=torch.tensor(0.), dist_reduce_fx='sum')
        self.add_state('roleplay_total', default=torch.tensor(0.), dist_reduce_fx='sum')
        self.add_state('writing_score', default=torch.tensor(0.), dist_reduce_fx='sum')
        self.add_state('writing_total', default=torch.tensor(0.), dist_reduce_fx='sum')

        self.client = None

    def init_openai(self):
        try:
            from openai import OpenAI
        except ImportError as e:
            raise MissingConditionalImportError(extra_deps_group='openai',
                                                conda_package='openai',
                                                conda_channel='conda-forge') from e
        self.client = OpenAI()

    def call_judge(self,
                   prompt_one,
                   prompt_two,
                   first_generation,
                   second_generation,
                   category,
                   reference_answer_one=None,
                   reference_answer_two=None) -> List[str]:
        # if sample_answer.startswith(' '):
        #     sample_answer = sample_answer.lstrip()

        if category == 'math':
            system_prompt = deepcopy(self.SINGLE_V1_MATH_MULTI_TURN_TEMPLATE_SYSTEM_PROMPT)
            template = deepcopy(self.SINGLE_V1_MATH_MULTI_TURN_TEMPLATE)
            formatted_template = template.format(question_1=prompt_one,
                                                 question_2=prompt_two,
                                                 answer_1=first_generation,
                                                 answer_2=second_generation,
                                                 ref_answer_1=reference_answer_one,
                                                 ref_answer_2=reference_answer_two)
        else:
            system_prompt = deepcopy(self.MULTI_TURN_SYSTEM_PROMPT)
            template = deepcopy(self.SINGLE_V1_MULTI_TURN_TEMPLATE)
            formatted_template = template.format(question_1=prompt_one,
                                                 question_2=prompt_two,
                                                 answer_1=first_generation,
                                                 answer_2=second_generation)

        response = self.client.chat.completions.create(model='gpt-4',
                                                       messages=[{
                                                           'role': 'system',
                                                           'content': system_prompt
                                                       }, {
                                                           'role': 'user',
                                                           'content': formatted_template
                                                       }],
                                                       max_tokens=250)

        return response.choices[0].message.content, formatted_template

    def update(self, batch: Dict[str, Any], outputs: List[str]):
        if not self.client:
            self.init_openai()
        for i, first_generation in enumerate(outputs['generation_one']):
            second_generation = outputs['generation_two'][i]
            prompt_one = batch['untokenized_prompt_one'][i]
            prompt_two = batch['untokenized_prompt_two'][i]
            result, formatted_template = self.call_judge(prompt_one=prompt_one,
                                                         prompt_two=prompt_two,
                                                         first_generation=first_generation,
                                                         second_generation=second_generation,
                                                         category=batch['category'][i],
                                                         reference_answer_one=batch['reference_answer_one'][i],
                                                         reference_answer_two=batch['reference_answer_two'][i])

            log.info('********* Formatted Response and Result: *********')
            log.info(formatted_template)
            log.info(result)
            score = None
            match = re.search(self.ONE_SCORE_PATTERN, result)
            if not match:
                match = re.search(self.ONE_SCORE_PATTERN_BACKUP, result)
            if match:
                score = ast.literal_eval(match.groups()[0])
                self.all_scores += torch.tensor(score)
                self.update_category_score(batch['category'][i], score)
            else:
                self.invalid_judge_response += 1
            self.total += 1
            if self.cache_responses:
                self.response_cache.append({'score': score, 'result': result, 'formatted_template': formatted_template})

        # OpenAI Client can't be copied by deepcopy and will throw an error, so we delete it after we use it
        # Initializatin takes ~12 ms
        del self.client
        self.client = None

    def update_category_score(self, category, score):
        if category == 'math':
            self.math_total += 1
            self.math_score += score
        elif category == 'writing':
            self.writing_total += 1
            self.writing_score += score
        elif category == 'roleplay':
            self.roleplay_total += 1
            self.roleplay_score += score
        elif category == 'reasoning':
            self.reasoning_total += 1
            self.reasoning_score += score
        elif category == 'coding':
            self.coding_total += 1
            self.coding_score += score
        elif category == 'extraction':
            self.extraction_total += 1
            self.extraction_score += score
        elif category == 'stem':
            self.stem_total += 1
            self.stem_score += score
        elif category == 'humanities':
            self.humanities_total += 1
            self.humanities_score += score

    def compute(self):
        super().compute()
        log.info(f'Math score:        {(self.math_score / self.math_total).item()}')
        log.info(f'Writing score:     {(self.writing_score / self.writing_total).item()}')
        log.info(f'Roleplay score:    {(self.roleplay_score / self.roleplay_total).item()}')
        log.info(f'Reasoning score:   {(self.reasoning_score / self.reasoning_total).item()}')
        log.info(f'Coding score:      {(self.coding_score / self.coding_total).item()}')
        log.info(f'Extraction score:  {(self.extraction_score / self.extraction_total).item()}')
        log.info(f'STEM score:        {(self.stem_score / self.stem_total).item()}')
        log.info(f'Humanities score:  {(self.humanities_score / self.humanities_total).item()}')
        log.info(f'Combined score:    {(self.all_scores / self.total).item()}')
        log.info(f'Total Questions:   {self.total.item()}')
        log.info(f'Invalid Responses: {self.invalid_judge_response.item()}')
        return self.all_scores / self.total

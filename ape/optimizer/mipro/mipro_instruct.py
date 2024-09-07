from collections import defaultdict
import os
import pickle
import random
import inspect
from typing import Any, Dict, List, Optional, Tuple, Union
from pydantic import BaseModel
import numpy as np
import optuna
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.prompt import Confirm
from ape.evaluate.evaluate import Evaluate
from ape.metric.metric_base import BaseMetric, GlobalMetric
from ape.metric import CosineSimilarityMetric, JsonMatchMetric, SemanticF1Metric, PydanticMatchMetric
from ape.optimizer.mipro.mipro_base import MIPROBase
from ape.optimizer.mipro.mipro_proposer import MIPROProposer
from ape.optimizer.utils import eval_candidate_prompt, find_best_fewshot, get_prompt_with_highest_avg_score, reformat_prompt, save_candidate_prompt
from ape.proposer.instruct_by_score import InstructByScore
from ape.proposer.utils import extract_prompt
from ape.utils import logger, run_async
from ape.prompt.prompt_base import Prompt
from ape.types import Dataset, ResponseFormat

BOOTSTRAPPED_FEWSHOT_EXAMPLES_IN_CONTEXT: int = 3
LABELED_FEWSHOT_EXAMPLES_IN_CONTEXT: int = 0

# Instruction Generation. Later Add Optuna and GroundedProposer to do (N+1) x (M+1) search.
class MIPROInstruct(MIPROBase):
    """
    MIPROInstruct class for optimizing prompts.

    This class is a modified vesion of MIPRO with a focus on generating better instructions. It supports minibatch evaluation,
    few-shot examples, and various optimization strategies.
    """
    gen_metric_description: Prompt = Prompt.from_filename("gen-metric-description")
    gen_metric_description_with_global_metric: Prompt = Prompt.from_filename("gen-metric-description-with-global-metric")
    merge_prompts: Prompt = Prompt.from_filename("gen-merged-prompt")

    def __init__(
        self,
        prompt_model: str,
        task_model: str,
        metric: BaseMetric,
        global_metric: Optional[GlobalMetric] = None,
        teacher_settings: Dict[str, Any] = {},
        num_candidates: int = 10,
        init_temperature: float = 1.0,
        verbose: bool = False,
        track_stats: bool = True,
        log_dir: Optional[str] = None,
        view_data_batch_size: int = 10,
        minibatch_size: int = 25,
        update_prompt_after_full_eval: bool = True,
        minibatch_full_eval_steps: int = 10
    ):
        """
        Initialize the MIPROInstruct optimizer.

        Args:
            prompt_model (str): The model used for generating prompts.
            task_model (str): The model used for executing the task.
            metric (BaseMetric): The metric object used for evaluation.
            global_metric (Optional[GlobalMetric]): The global metric object for overall evaluation.
            teacher_settings (Dict[str, Any]): Settings for the teacher model.
            num_candidates (int): Number of candidate prompts to generate.
            init_temperature (float): Initial temperature for sampling.
            verbose (bool): Whether to print verbose output.
            track_stats (bool): Whether to track statistics during optimization.
            log_dir (Optional[str]): Directory for logging.
            view_data_batch_size (int): Batch size for viewing data.
            minibatch_size (int): Size of minibatches for evaluation.
            update_prompt_after_full_eval (bool): Whether to update the best prompt after full evaluation.
            minibatch_full_eval_steps (int): Number of steps for full evaluation on minibatches.
        """
        
        super().__init__(
            prompt_model=prompt_model,
            task_model=task_model,
            teacher_settings=teacher_settings,
            num_candidates=num_candidates,
            metric=metric,
            global_metric=global_metric,
            init_temperature=init_temperature,
            verbose=verbose,
            track_stats=track_stats,
            log_dir=log_dir,
            view_data_batch_size=view_data_batch_size,
            minibatch_size=minibatch_size,
            update_prompt_after_full_eval=update_prompt_after_full_eval,
            minibatch_full_eval_steps=minibatch_full_eval_steps
        )
        
    def _get_batch_size(self, minibatch: bool, trainset: Dataset) -> int:
        return self.minibatch_size if minibatch else len(trainset)

    def _display_warning_and_confirm(
        self,
        trainset: Dataset,
        max_steps: int,
        minibatch: bool,
        requires_permission_to_run: bool,
    ) -> bool:
        """
        Display a warning about projected LM calls and costs, and optionally ask for user confirmation.

        Args:
            trainset (Dataset): The training dataset.
            max_steps (int): Maximum number of optimization steps.
            minibatch (bool): Whether minibatch optimization is used.
            requires_permission_to_run (bool): Whether user confirmation is required.

        Returns:
            bool: True if the user confirms or confirmation is not required, False otherwise.
        """
        console = Console()

        estimated_prompt_model_calls = 10 + self.num_candidates + 1

        # TODO: Modify to match the actual number of LM calls in the program.
        if not minibatch:
            estimated_task_model_calls = len(trainset) * max_steps
            task_model_line = f"- Task Model: {len(trainset)} examples in train set * {max_steps} batches * # of LM calls in your program = ({estimated_task_model_calls} * # of LM calls in your program) task model calls"
        else:
            if self.update_prompt_after_full_eval:
                estimated_task_model_calls = self.minibatch_size * max_steps + (
                    len(trainset) * (max_steps // self.minibatch_full_eval_steps)
                )
                task_model_line = f"- Task Model: {self.minibatch_size} examples in minibatch * {max_steps} batches + {len(trainset)} examples in train set * {max_steps // self.minibatch_full_eval_steps} full evals = {estimated_task_model_calls} task model calls"
            else:
                estimated_task_model_calls = self.minibatch_size * max_steps
                task_model_line = f"- Task Model: {self.minibatch_size} examples in minibatch * {max_steps} batches = {estimated_task_model_calls} task model calls"

        warning_text = Text.from_markup(
            f"[bold yellow]WARNING: Projected Language Model (LM) Calls[/bold yellow]\n\n"
            f"Please be advised that based on the parameters you have set, the maximum number of LM calls is projected as follows:\n\n"
            f"[cyan]- Prompt Model: 10 data summarizer calls + {self.num_candidates} lm calls in program = {estimated_prompt_model_calls} prompt model calls[/cyan]\n"
            f"[cyan]{task_model_line}[/cyan]\n\n"
            f"[bold yellow]Estimated Cost Calculation:[/bold yellow]\n"
            f"Total Cost = (Number of calls to task model * (Avg Input Token Length per Call * Task Model Price per Input Token + Avg Output Token Length per Call * Task Model Price per Output Token) \n"
            f"            + (Number of calls to prompt model * (Avg Input Token Length per Call * Task Prompt Price per Input Token + Avg Output Token Length per Call * Prompt Model Price per Output Token).\n\n"
            f"For a preliminary estimate of potential costs, we recommend you perform your own calculations based on the task and prompt models you intend to use. "
            f"If the projected costs exceed your budget or expectations, you may consider:\n"
            f"- Reducing the number of trials (`max_steps`), the size of the trainset, or the number of LM calls in your program.\n"
            f"- Using a cheaper task model to optimize the prompt."
        )

        console.print(Panel(warning_text, title="Cost Warning", expand=False))

        if requires_permission_to_run:
            return Confirm.ask("Do you wish to continue?")
        return True
    
    async def generate_metric_description(self) -> str:
        """
        Generate a description of the metric.
        """
        try:
            if isinstance(self.metric, CosineSimilarityMetric):
                metric_str = "Measures how similar the predicted text is to the correct answer by comparing their vector representations. A higher score means the prediction is more similar to the gold."
            elif isinstance(self.metric, JsonMatchMetric):
                metric_str = "Compares JSON format predictions with the correct JSON answer. It checks if each key-value pair in the prediction matches the ground truth exactly. The score reflects how many pairs match correctly."
            elif isinstance(self.metric, SemanticF1Metric):
                metric_str = """\
        Evaluates how well the prediction captures the meaning of the correct answer:
        1. Extracts key statements from both the prediction and ground truth.
        2. Checks how many statements from the prediction are found in the ground truth (Precision).
        3. Checks how many statements from the ground truth are found in the prediction (Recall).
        4. Calculates the F1 score, which balances Precision and Recall. A higher score indicates better semantic matching."""
            elif isinstance(self.metric, PydanticMatchMetric):
                metric_str = "Compares structured data (in JSON format) between the prediction and the correct answer. It checks if each field and its value in the prediction matches the ground truth exactly. The score indicates how closely the prediction's structure matches the expected format."
            else:
                compute_function = getattr(self.metric, "compute", None)
                compute_function_source_code = inspect.getsource(compute_function)
                
                if self.global_metric:
                    global_metric_compute_function = getattr(self.global_metric, "compute", None)
                    global_metric_compute_function_source_code = inspect.getsource(global_metric_compute_function)
                    
                    # get Prompt gen-metric-description-with-global-metric.prompt
                    metric_str = await self.gen_metric_description_with_global_metric(
                        **{
                            "metric_sourcecode": compute_function_source_code,
                            "global_metric_sourcecode": global_metric_compute_function_source_code,
                        }
                    )

                else:
                    metric_str = await self.gen_metric_description(
                        **{
                            "metric_sourcecode": compute_function_source_code,
                        }
                    )
                return metric_str
        except Exception as e:
            logger.error(f"Error generating metric description: {e}")
            return ""

    async def generate_instructions(
        self,
        student: Prompt,
        *,
        trainset: Dataset,
        testset: Optional[Dataset] = None,
        eval_kwargs: Optional[Dict[str, Any]] = {},
        seed: int = 9,
        response_format: Optional[ResponseFormat] = None,
        log_dir: str,
    ) -> Optional[Prompt]:
        """
        Optimize the given prompt using MIPRO (Multi-prompt Instruction PRoposal Optimizer).

        This method generates and evaluates multiple prompt candidates, using optuna for hyperparameter optimization.
        It supports minibatch evaluation, fewshot examples, and various optimization strategies.

        Returns:
            Optional[Prompt]: The best performing prompt, or None if optimization is aborted.
        """

        random.seed(seed)
        np.random.seed(seed)
        testset = testset or trainset

        if response_format is not None:
            student = await reformat_prompt(
                prompt=student, response_format=response_format
            )

        metric_str = await self.generate_metric_description(
            metric=self.metric,
            global_metric=self.global_metric,
        )
        print(f"Metric description: {metric_str}")

        evaluate: Evaluate = Evaluate(
            testset=testset,
            metric=self.metric,
            global_metric=self.global_metric,
            **eval_kwargs,
        )
        
        # This is the proposer.
        proposer = InstructByScore(
            prompt_model=self.prompt_model,
            trainset=trainset,
            view_data_batch_size=self.view_data_batch_size,
        )
        
        print("Proposer ready")

        logger.info(f"Generating {self.num_candidates} explore instruction candidates")
        
        # Generate N candidates using InstructByScore.
        explore_prompt_candidates = await proposer.propose_prompts(
            base_prompt=student,
            N=self.num_candidates,
            T=self.init_temperature,
            trial_logs={},
            evaluate=evaluate,
            metric=metric_str,
        )

        if log_dir:
            if not os.path.exists(log_dir):
                os.makedirs(log_dir)
            with open(os.path.join(log_dir, "instructions_to_save.pkl"), "wb") as file:
                pickle.dump(explore_prompt_candidates, file)

        instruction_candidates: List[List[Dict[str, str]]] = [
            prompt.messages for prompt in explore_prompt_candidates
        ]

        return instruction_candidates

    async def optimize(
        self,
        student: Prompt,
        *,
        task_description: str = "",
        trainset: Dataset,
        testset: Optional[Dataset] = None,
        max_steps: int = 30,
        max_bootstrapped_demos: int = 5,
        max_labeled_demos: int = 2,
        goal_score: float = 100,
        eval_kwargs: Optional[Dict[str, Any]] = None,
        seed: int = 9,
        minibatch: bool = True,
        requires_permission_to_run: bool = True,
        response_format: Optional[ResponseFormat] = None,
        log_dir: str,
    ) -> Optional[Prompt]:
        """
        Optimize the given prompt using MIPRO (Multi-prompt Instruction PRoposal Optimizer).

        This method generates and evaluates multiple prompt candidates, using optuna for hyperparameter optimization.
        It supports minibatch evaluation, fewshot examples, and various optimization strategies.

        Returns:
            Optional[Prompt]: The best performing prompt, or None if optimization is aborted.
        """
        eval_kwargs = eval_kwargs or {}
        if not self._display_warning_and_confirm(
            trainset, max_steps, minibatch, requires_permission_to_run
        ):
            logger.info("Optimization aborted by the user.")
            return None

        random.seed(seed)
        np.random.seed(seed)
        testset = testset or trainset

        if response_format is not None:
            student = await reformat_prompt(
                prompt=student, response_format=response_format
            )
            
        max_bootstrapped_demos_for_candidate_gen: int = (
            BOOTSTRAPPED_FEWSHOT_EXAMPLES_IN_CONTEXT
            if max_bootstrapped_demos == 0 and max_labeled_demos == 0
            else max_bootstrapped_demos
        )
        max_labeled_demos_for_candidate_gen: int = (
            LABELED_FEWSHOT_EXAMPLES_IN_CONTEXT
            if max_bootstrapped_demos == 0 and max_labeled_demos == 0
            else max_labeled_demos
        )
        
        evaluate: Evaluate = Evaluate(
            testset=testset, metric=self.metric, global_metric=self.global_metric, **eval_kwargs
        )
        
        # find best few-shot
        best_fewshot, best_score = await find_best_fewshot(
            student=student,
            num_candidate_sets=self.num_candidates,
            trainset=trainset,
            max_labeled_demos=max_labeled_demos_for_candidate_gen,
            max_bootstrapped_demos=max_bootstrapped_demos_for_candidate_gen,
            metric=self.metric,
            teacher_settings=self.teacher_settings,
            seed=seed,
            evaluate=evaluate,
        )
        
        print(f"best_fewshot: {best_fewshot}")

        score_based_proposer = InstructByScore(
            prompt_model=self.prompt_model,
            trainset=trainset,
            view_data_batch_size=self.view_data_batch_size,
            use_tip=True,
        )

        logger.info(f"Generating {self.num_candidates} explore instruction candidates")
        
        metric_str = await self.generate_metric_description(
            metric=self.metric,
            global_metric=self.global_metric,
        )
        
        logger.info(f"Metric description: {metric_str}")
        
        # Generate N candidates using InstructByScore.
        score_based_prompt_candidates = await score_based_proposer.propose_prompts(
            base_prompt=student,
            N=self.num_candidates,
            T=self.init_temperature,
            trial_logs={},
            evaluate=evaluate,
            metric=metric_str,
        )
        
        logger.info(f"finished generating score based prompt candidates")
        
        score_based_instruction_candidates: List[List[Dict[str, str]]] = [
            prompt.messages for prompt in score_based_prompt_candidates
        ]

        if log_dir:
            if not os.path.exists(log_dir):
                os.makedirs(log_dir)
            with open(os.path.join(log_dir, "score_based_instructions_to_save.pkl"), "wb") as file:
                pickle.dump(score_based_prompt_candidates, file)
        
        
        format_based_proposer: MIPROProposer = MIPROProposer(**self.model_dump())
        format_based_prompt_candidates: List[Prompt] = await format_based_proposer.generate_candidates(
            prompt=student,
            trainset=trainset,
            task_description=task_description,
            response_format=response_format,
        )
        logger.info("finished generating format based prompt candidates")
        
        if log_dir:
            with open(os.path.join(log_dir, "format_based_instructions_to_save.pkl"), "wb") as file:
                pickle.dump(format_based_prompt_candidates, file)

        format_based_instruction_candidates: List[List[Dict[str, str]]] = [
            prompt.messages for prompt in format_based_prompt_candidates
        ]

        best_score: float = float("-inf")
        best_prompt: Optional[Prompt] = None
        trial_logs: Dict[int, Dict[str, Any]] = {}
        total_eval_calls: int = 0
        param_score_dict: Dict[str, List[Tuple[float, Prompt]]] = defaultdict(list)
        fully_evaled_param_combos: Dict[str, Dict[str, Union[Prompt, float]]] = {}

        def objective(trial: optuna.Trial) -> float:
            nonlocal best_prompt, best_score, trial_logs, total_eval_calls, param_score_dict, fully_evaled_param_combos

            logger.info(f"Starting trial num: {trial.number}")
            trial_logs[trial.number] = {}

            candidate_prompt: Prompt = student.deepcopy()

            score_based_instruction_idx: int = trial.suggest_categorical(
                "score_based_instruction", range(len(score_based_instruction_candidates))
            )
            chosen_params: List[int] = [score_based_instruction_idx]

            format_based_instruction_idx: int = trial.suggest_categorical(
                "format_based_instruction", range(len(format_based_instruction_candidates))
            )
            chosen_params.append(format_based_instruction_idx)

            trial_logs[trial.number].update(
                {
                    "score_based_instruction": score_based_instruction_idx,
                    "format_based_instruction": format_based_instruction_idx,
                }
            )
            
            score_based_candidate_prompt = score_based_instruction_candidates[score_based_instruction_idx]
            format_based_candidate_prompt = format_based_instruction_candidates[format_based_instruction_idx]
            
            merged_candidate_prompt_text = self.merge_prompts(
                score_based_candidate_prompt,
                format_based_candidate_prompt,
            )
            
            merged_candidate_prompt = extract_prompt(merged_candidate_prompt_text)
            merged_candidate_prompt = Prompt.load(merged_candidate_prompt)
            
            candidate_prompt.messages = merged_candidate_prompt.messages
            candidate_prompt.fewshot = best_fewshot
            
            print(candidate_prompt)

            trial_logs[trial.number]["prompt_path"] = save_candidate_prompt(
                candidate_prompt, log_dir, trial.number
            )

            batch_size: int = self._get_batch_size(minibatch, trainset)
            score: float = run_async(
                eval_candidate_prompt(batch_size, trainset, candidate_prompt, evaluate)
            )

            categorical_key: str = ",".join(map(str, chosen_params))
            param_score_dict[categorical_key].append((score, candidate_prompt))

            trial_logs[trial.number].update(
                {
                    "num_eval_calls": batch_size,
                    "full_eval": batch_size >= len(trainset),
                    "score": score,
                    "pruned": False,
                    "total_eval_calls_so_far": total_eval_calls + batch_size,
                }
            )
            total_eval_calls += batch_size

            if self.update_prompt_after_full_eval:
                if (
                    score > best_score
                    and trial_logs[trial.number]["full_eval"]
                    and not minibatch
                ):
                    best_score = score
                    best_prompt = candidate_prompt.deepcopy()

                if minibatch and (
                    trial.number % self.minibatch_full_eval_steps == 0
                    or trial.number == max_steps - 1
                ):
                    trial_logs[trial.number]["mb_score"] = score
                    trial_logs[trial.number]["mb_prompt_path"] = trial_logs[
                        trial.number
                    ]["prompt_path"]

                    highest_mean_prompt, combo_key = get_prompt_with_highest_avg_score(
                        param_score_dict, fully_evaled_param_combos
                    )
                    full_train_score: float = run_async(
                        eval_candidate_prompt(
                            len(trainset), trainset, highest_mean_prompt, evaluate
                        )
                    )

                    fully_evaled_param_combos[combo_key] = {
                        "program": highest_mean_prompt,
                        "score": full_train_score,
                    }
                    total_eval_calls += len(trainset)
                    trial_logs[trial.number].update(
                        {
                            "total_eval_calls_so_far": total_eval_calls,
                            "full_eval": True,
                            "prompt_path": save_candidate_prompt(
                                prompt=highest_mean_prompt,
                                log_dir=log_dir,
                                trial_num=trial.number,
                                note="full_eval",
                            ),
                            "score": full_train_score,
                        }
                    )

                    if full_train_score > best_score:
                        best_score = full_train_score
                        best_prompt = highest_mean_prompt.deepcopy()
            else:
                if score > best_score:
                    best_score = score
                    best_prompt = candidate_prompt.deepcopy()

            if score >= goal_score:
                trial.study.stop()

            return score
        
        logger.info("starting optuna study")

        study: optuna.Study = optuna.create_study(
            direction="maximize",
            sampler=optuna.samplers.TPESampler(seed=seed, multivariate=True),
        )
        study.optimize(objective, n_trials=max_steps)
        logger.info("finished optuna study")
        if best_prompt is not None and self.track_stats:
            best_prompt.metadata["trial_logs"] = trial_logs
            best_prompt.metadata["score"] = best_score
            best_prompt.metadata["total_eval_calls"] = total_eval_calls
            
        if log_dir:
            with open(
                os.path.join(log_dir, "best_prompt.prompt"), "w", encoding="utf-8"
            ) as f:
                f.write(best_prompt.dump())

            with open(os.path.join(log_dir, "optuna_study.pkl"), "wb") as file:
                pickle.dump(study, file)

        return best_prompt

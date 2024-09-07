import asyncio
import json
import random
from re import M
from typing import Any, Dict, List, Optional

from ape.evaluate.evaluate import Evaluate
from ape.prompt.prompt_base import Prompt
from ape.prompt.utils import format_fewshot
from ape.proposer.dataset_summary_generator import create_dataset_summary
from ape.proposer.utils import extract_prompt
from ape.proposer.propose_base import Proposer
from ape.types.response_format import (
    ResponseFormat,
)
from ape.utils import logger
from ape.types import Dataset, EvaluationResult

# TIPS for generating new instructions.
# TODO: Refine these tips.
TIPS = {
    "none": "Make it better",
    "one_side_first": "Try to optimize a certain part of the evaluation score first. For example, if the evaluation is based on both precision and recall, try to optimize for recall first. Be extreme at times.",
    "double_down": "Find what the prompt is already doing well and double down on it. For example, if the prompt already does a good job with precision, try to improve that even further with various strategies. Be extreme at times",
    "weakness_first": "Find what the prompt is already doing poorly based on the evaluation score and metric and try to improve that. For example, if the prompt has low recall, try to improve that with various strategies. Be extreme at times",
    "think": "Try to think about what the evaluation score and metric are actually measuring. Then, try to optimize for that. For example, if the evaluation score is calculated based on the average length of the generated instructions, try to optimize for that. Be extreme at times",
}


class InstructByScore(Proposer):
    def __init__(
        self,
        prompt_model: str,
        trainset: Dataset,
        use_dataset_summary=True,
        use_task_demos=True,
        use_tip=True,
        view_data_batch_size=10,
    ):
        """
        Initialize the GroundedProposer.

        Args:
            prompt_model (str): The name of the prompt model to use.
            trainset (Dataset): The training dataset.
            use_dataset_summary (bool, optional): Whether to use dataset summary. Defaults to True.
            use_task_demos (bool, optional): Whether to use task demonstrations. Defaults to True.
            use_tip (bool, optional): Whether to include tips. Defaults to True.
            set_tip_randomly (bool, optional): Whether to randomly select tips. Defaults to True.
            view_data_batch_size (int, optional): The batch size for viewing data. Defaults to 10.
        """
        self.use_dataset_summary = use_dataset_summary
        self.use_task_demos = use_task_demos
        # self.use_instruct_history = use_instruct_history
        self.use_tip = use_tip

        self.trainset: Dataset = trainset
        self.prompt_model = prompt_model
        self.view_data_batch_size = view_data_batch_size
        self.describe_prompt = Prompt.from_filename("describe-prompt")
        self.generate_instructions = Prompt.from_filename("gen-instruction-with-eval")
        self.describe_prompt.model = prompt_model
        self.generate_instructions.model = prompt_model
        self.data_summary = None

    async def prepare_dataset_summary(
        self,
        view_data_batch_size=10,
    ):
        """
        Prepare a summary of the dataset.

        Args:
            view_data_batch_size (int, optional): The batch size for viewing data. Defaults to 10.
        """
        self.view_data_batch_size = view_data_batch_size
        logger.info("Preparing dataset summary")
        self.data_summary = await create_dataset_summary(
            trainset=self.trainset,
            view_data_batch_size=self.view_data_batch_size,
            prompt_model=self.prompt_model,
        )
        logger.info(f"DATA SUMMARY: {self.data_summary}")

    async def propose_prompts(
        self,
        trial_logs: Dict[str, Any],
        N: int,
        T: float,
        evaluate: Evaluate,
        base_prompt: Optional[Prompt] = None,
        metric: Optional[str] = None,
    ) -> List[Prompt]:
        """
        Propose a set of new instructions for the task based on specified criteria.

        Args:
            trial_logs (Dict[str, Any]): Logs from previous trials.
            N (int): Number of proposals to generate.
            T (float): Temperature for generation.
            evaluate (Evaluate): Evaluation function.
            base_prompt (Optional[Prompt], optional): Base prompt to start from. Defaults to None.
            metric (Optional[str], optional): Metric to use for evaluation. Defaults to None.

        Returns:
            List[Prompt]: A list of proposed prompts.
        """
        # This is not necessary. But I kept it here for now.
        if not self.data_summary and self.trainset:
            await self.prepare_dataset_summary()

        # This is the part where we evaluate the base prompt.
        base_evaluation_score, base_evaluation_result = str(await evaluate(base_prompt, return_outputs=True))
    
        proposed_instructions = []

        # Generate N new instructions based on evaluation result.
        for i in range(N):
            new_instruction = await self.generate_new_instruction(
                index=i,
                base_prompt=base_prompt,
                # evaluation_score=base_evaluation_score,
                evaluation_result=base_evaluation_score,
                T=T,
                response_format=base_prompt.response_format,
                metric=metric,
            )
            proposed_instructions.append(new_instruction)

        return proposed_instructions

    async def generate_new_instruction(
        self,
        index: int,
        base_prompt: Prompt,
        # evaluation_score: str,
        evaluation_result: List[EvaluationResult],
        T: float,
        response_format: Optional[ResponseFormat] = None,
        metric: Optional[str] = None,
        retry_count: int = 0,
    ) -> Prompt:
        """
        Generate a new instruction based on the base prompt and its evaluation result.

        Args:
            base_prompt (Prompt): The base prompt.
            evaluation_score (str): The score of the base prompt.
            evaluation_result (List[EvaluationResult]): The result of evaluating the base prompt.
            T (float): Temperature for generation.
            response_format (Optional[ResponseFormat], optional): Format for the response.
            metric (Optional[str], optional): Metric used for evaluation.
            retry_count (int): 현재 재시도 횟수

        Returns:
            Prompt: A new proposed prompt or the base prompt if generation fails after 3 attempts.
        """

        # Select a tip
        if self.use_tip:
            selected_tip = list(TIPS.values())[index % len(TIPS)]
        else:
            selected_tip = ""

        base_prompt_messages = [
            json.dumps(message) for message in base_prompt.messages
        ]
        base_prompt_messages_str = "\n".join(base_prompt_messages)

        try:
            # Generate new instruction from Prompt.from_filename("gen-instruction-with-eval")
            # input : base_prompt, evaluation_result, evaluation_function, tip, response_format
            new_instruction_text = await self.generate_instructions(
                base_prompt=base_prompt_messages_str,
                evaluation_result=str(evaluation_result),
                evaluation_function=metric,
                tip=selected_tip,
                response_format=(
                    str(response_format.get("type"))
                    if isinstance(response_format, dict)
                    and response_format.get("type") != "json_schema"
                    else (
                        str(response_format.get("json_schema", {}).get("schema"))
                        if isinstance(response_format, dict)
                        else str(response_format)
                    )
                ),
            )
            
            # print(f"New Instruction: {new_instruction_text}")
            extracted_prompt = extract_prompt(new_instruction_text)

            # Create a new Prompt object with the generated instruction
            new_prompt = Prompt.load(extracted_prompt)
            if not new_prompt.messages:
                raise ValueError("Generated prompt has no messages")
            new_prompt.model = self.prompt_model
            new_prompt.response_format = response_format
            new_prompt.name = "InstructNew"

        except Exception as e:
            print(f"Error generating new instruction (attempt {retry_count + 1}): {e}")
            if retry_count < 2:  # 최대 3번 시도 (0, 1, 2)
                return await self.generate_new_instruction(
                    index=index,
                    base_prompt=base_prompt,
                    evaluation_result=evaluation_result,
                    T=T,
                    response_format=response_format,
                    metric=metric,
                    retry_count=retry_count + 1,
                )
            else:
                print("Failed to generate new instruction after 3 attempts. Returning base prompt.")
                return base_prompt

        return new_prompt

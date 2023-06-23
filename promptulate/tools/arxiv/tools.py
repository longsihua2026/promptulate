import re
import time
from pydantic import BaseModel, Field
from typing import List, Dict, Optional, Union, Tuple
from broadcast_service import broadcast_service

from promptulate.llms.base import BaseLLM
from promptulate.llms.openai import OpenAI
from promptulate.utils.logger import get_logger
from promptulate.utils.core_utils import record_time, listdict_to_string
from promptulate.tools.base import BaseTool
from promptulate.tools.arxiv.api_wrapper import ArxivAPIWrapper

logger = get_logger()


class ArxivQueryTool(BaseTool):
    name = "arxiv-query"
    description = (
        "A query tool around Arxiv.org "
        "Useful for when you need to answer questions about Physics, Mathematics, "
        "Computer Science, Quantitative Biology, Quantitative Finance, Statistics, "
        "Electrical Engineering, and Economics "
        "from scientific articles on arxiv.org. "
        "Input should be a search query."
    )
    api_wrapper: ArxivAPIWrapper = Field(default_factory=ArxivAPIWrapper)

    def run(self, query: str, *args, **kwargs) -> str:
        kwargs.update({"from_callback": self.name})
        return listdict_to_string(
            self.api_wrapper.query(query, **kwargs), is_wrap=False
        )


def _init_arxiv_reference_tool_llm():
    preset = "你是一个Arxiv助手，你的任务是帮助使用者提供一些论文方面的建议，并且遵循用户的指令输出。"
    return OpenAI(preset_description=preset)


class ArxivReferenceTool(BaseTool):
    name = "arxiv-reference"
    description = (
        "Use this tool to find search related to the field."
        "Your input is a arxiv keyword query."
    )
    llm: BaseLLM = Field(default_factory=_init_arxiv_reference_tool_llm)
    max_reference_num = 3
    reference_string: str = ""
    reference_counter: int = 0

    @record_time()
    def run(self, query: str, *args, **kwargs) -> Union[List[Dict], str]:
        """
        Input arxiv paper information and return paper references.
        Args:
            query(str): arxiv paper information
            *args: Nothing
            **kwargs:
                return_type(Optional[str]): return string default. If you want to return List[Dict] type data,
                you can set 'return_type'='original'
        Returns:
            string type reference information
        """

        @broadcast_service.on_listen("ArxivReferenceTool.get_relevant_paper_info")
        @record_time()
        def get_relevant_paper_info(keyword: str):
            """return paper related information from keyword"""
            arxiv_query_tool = ArxivQueryTool()
            specified_fields = ["entry_id", "title"]
            queryset = arxiv_query_tool.run(
                keyword, num_results=6, specified_fields=specified_fields
            )
            self.reference_string += queryset
            self.reference_counter += 1

        def analyze_query_string(query_string: str) -> List[str]:
            """analyze `[query]: keyword1, keyword2, keyword3` type data to return keywords list"""
            assert "[query]" in query_string
            query_string = query_string.split(":")[1]
            return query_string.split(",")

        def analyze_reference_string(reference_string: str) -> List[Dict]:
            pattern = r"\[(\d+)\]\s+(.+?),\s+(http://.+?);"
            references = []
            for match in re.finditer(pattern, reference_string):
                references.append({"title": match.group(2), "url": match.group(3)})
            return references

        self.reference_counter = 0
        prompt = (
            f"现在你需要根据其研究的具体内容，列出至少{self.max_reference_num}篇参考文献，你可以使用arxiv进行查询，你需要给我提供3个arxiv查询关键词，我将使用"
            f"arxiv进行查询，你需要根据我返回的结果选取最符合当前研究的{self.max_reference_num}篇参考文献。你的输出必须是三个查询词。\n"
            "如 [query]: keyword1, keyword2, keyword3\n"
            f"用户输入:{query}"
        )
        keywords = analyze_query_string(self.llm(prompt))
        for keyword in keywords:
            broadcast_service.broadcast(
                "ArxivReferenceTool.get_relevant_paper_info", keyword
            )

        while self.reference_counter < 3:
            time.sleep(0.2)

        prompt = (
            "现在你需要根据下面给出的论文，返回最合适的5篇参考文献\n"
            f"```{self.reference_string}```\n"
            "你的输出格式必须为\n[1] [title1](url1);\n[2] [title2](url2);\n[3] [title3](url3);除此之外，不能输出任何其他内容。"
        )
        # todo If there is a problem with the returned format and an error is reported, then ask LLM to format the data
        result = self.llm(prompt)
        logger.debug(f"[promptulate ArxivReferenceTool response] {result}")
        if "return_type" in kwargs and kwargs["return_type"] == "original":
            return analyze_reference_string(result)
        return result


class ArxivSummaryTool(BaseTool):
    """An arxiv paper summary tool that passes in the article name (or arxiv id) and returns summary results

    Return:
        - Summary of the paper
        - List the key insights and lessons learned in the paper
        - List at least 5 references related to the research field of the paper
    """

    name = "arxiv-summary"
    description = (
        "A summary tool that can be used to obtain a paper summary, listing "
        "key insights and lessons learned in the paper, and references in the paper"
        "Your input is a arxiv keyword query."
    )
    llm: BaseLLM = Field(default_factory=OpenAI)
    api_wrapper: ArxivAPIWrapper = Field(default_factory=ArxivAPIWrapper)
    summary_string: str = ""
    summary_counter: int = 0

    def run(self, query: str, *args, **kwargs) -> str:
        @broadcast_service.on_listen("ArxivSummaryTool.summary")
        def get_opinion():
            prompt = (
                f"请就下面的论文摘要，列出论文中的关键见解和由论文得出的经验教训，你的输出需要分点给出 ```{paper_summary}```"
                "你的输出格式为:\n关键见解:\n{分点给出关键见解}\n经验教训:\n{分点给出经验教训}，用`-`区分每点，用中文输出"
            )

            opinion = self.llm(prompt)
            self.summary_string += opinion + "\n"
            logger.debug(f"[ArxivSummaryTool summary] {self.summary_string}")
            self.summary_counter += 1

        @broadcast_service.on_listen("ArxivSummaryTool.summary")
        def get_references():
            arxiv_referencer_tool = ArxivReferenceTool()
            references = arxiv_referencer_tool.run(paper_summary)
            self.summary_string += references + "\n"
            logger.debug(f"[ArxivSummaryTool summary] {self.summary_string}")
            self.summary_counter += 1

        @broadcast_service.on_listen("ArxivSummaryTool.summary")
        def get_opinion():
            prompt = (
                f"请就下面的论文摘要，为其相关主题或未来研究方向提供3-5个建议，你的输出需要分点给出  ```{paper_summary}```"
                "你的输出格式为:\n相关建议:\n{分点给出相关建议}，用`-`区分每点"
            )
            opinion = self.llm(prompt)
            self.summary_string += opinion + "\n"
            logger.debug(f"[ArxivSummaryTool summary] {self.summary_string}")
            self.summary_counter += 1

        paper_summary = listdict_to_string(
            self.api_wrapper.query(
                query, num_results=1, specified_fields=["title", "summary"]
            ),
            item_suffix="\n",
        )
        self.summary_string = paper_summary
        logger.debug(f"[ArxivSummaryTool summary] {self.summary_string}")
        broadcast_service.publish("ArxivSummaryTool.summary")

        while self.summary_counter < 3:
            time.sleep(0.2)

        return self.summary_string

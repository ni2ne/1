import asyncio
import re
import time
from typing import Dict,List,Type

from metagpt.actions.action import Action
from metagpt.actions.action_node import ActionNode
from metagpt.logs import logger
from metagpt.roles import Role
from metagpt.schema import Message
from metagpt.utils.common import OutputParser
from metagpt.const import TUTORIAL_PATH
from datetime import datetime
from metagpt.utils.file import File


STRUCT_WRITE_INSTRUCTION = """
您现在是短篇小说领域经验丰富的小说作家, 我们需要您根据给定的小说题材生成故事的基本结构。
按照以下内容输出符合给定题材的小说基本结构：
标题:"小说的标题"
设置:"小说的情景设置细节，包括时间段、地点和所有相关背景信息"
主角:"小说主角的名字、年龄、职业，以及他们的性格和动机、简要的描述"
反派角色:"小说反派角色的名字、年龄、职业，以及他们的性格和动机、简要的描述"
冲突:"小说故事的主要冲突，包括主角面临的问题和涉及的利害关系"
对话:"以对话的形式描述情节，揭示人物，以此提供一些提示给读者"
主题:"小说中心主题，并说明如何在整个情节、角色和背景中展开"
基调:"整体故事的基调，以及保持背景和人物的一致性和适当性的说明"
节奏:"调节故事节奏以建立和释放紧张气氛，推进情节，创造戏剧效果的说明"
其它:"任何额外的细节或对故事的要求，如特定的字数或题材限制"
"""

# ActionNode，写小说基本结构
STRUCT_WRITE = ActionNode(
    # ActionNode的名称
    key="Struct Write",
    # 期望输出的格式
    expected_type=str,
    # 命令文本
    instruction=STRUCT_WRITE_INSTRUCTION,
    # 例子输入，在这里我们可以留空
    example="",
 )

DIRECTORY_WRITE_INSTRUCTION = """
您现在是短篇小说领域经验丰富的小说作家。我们需要您根据context的内容创作出小说的目录和章节内容概况。
请按照以下要求提供该小说的具体目录和目录中的故事概况：
1. 输出必须严格符合指定语言。
2. 回答必须严格按照字典格式，如: {"title": "xxx", "directory": {"第一章：章节标题": "故事概况", "第二章：章节标题": "故事概况"}} 。
3. 目录应尽可能引人注目和充分，包括一级目录和本章故事概况。
4. 不要有额外的空格或换行符。
"""

# ActionNode，生成小说目录
DIRECTORY_WRITE = ActionNode(
    # ActionNode的名称
    key="Directory Write",
    # 期望输出的格式
    expected_type=str,
    # 命令文本
    instruction=DIRECTORY_WRITE_INSTRUCTION,
    # 例子输入，在这里我们可以留空
    example="",
 )

# ActionNode，写小说章节内容
STORY_WRITE = ActionNode(
    # ActionNode的名称
    key="Story Write",
    # 期望输出的格式
    expected_type=str,
    # 命令文本
    instruction="您现在是短篇小说领域经验丰富的小说作家。我们需要您创作出小说详细内容。",
    # 例子输入，在这里我们可以留空
    example="",
 )

class OUTLINE_WRITE_NODES(ActionNode):
    def __init__(self, name="Outline Nodes", expected_type=str, instruction="", example=""):
        super().__init__(key=name, expected_type=expected_type, instruction=instruction, example=example)
        self.add_children([STRUCT_WRITE, DIRECTORY_WRITE])    # 初始化过程，将上面实现的两个子节点加入作为OUTLINE_WRITE_NODES类的子节点

    async def fill(self, context, llm, schema="raw", mode="auto", strgy="complex"):
        self.set_llm(llm)
        self.set_context(context)
        if self.schema:
            schema = self.schema

        if strgy == "simple":
            return await self.simple_fill(schema=schema, mode=mode)
        elif strgy == "complex":
            # 这里隐式假设了拥有children
            child_context = context    # 输入context作为第一个子节点的context
            for _, i in self.children.items():
                i.set_context(child_context)    # 为子节点设置context
                child = await i.simple_fill(schema=schema, mode=mode)
                child_context = child.content    # 将返回内容（child.content）作为下一个子节点的context

            self.content = child_context    # 最后一个子节点返回的内容设置为父节点返回内容（self.content）
            return self


class CONTENT_WRITE_NODES(ActionNode):
    def __init__(self, name="Content Nodes", expected_type=str, instruction="", example=""):
        super().__init__(key=name, expected_type=expected_type, instruction=instruction, example=example)
        self.add_children([STORY_WRITE])  ## 这里只初始化了一个子节点

    async def fill(self, context, llm, schema="raw", mode="auto", strgy="complex"):
        self.set_llm(llm)
        self.set_context(context)
        if self.schema:
            schema = self.schema

        if strgy == "simple":
            return await self.simple_fill(schema=schema, mode=mode)
        elif strgy == "complex":
            # 这里隐式假设了拥有children
            child_context = context  # 输入context作为第一个子节点的context
            for _, i in self.children.items():
                i.set_context(child_context)  # 为子节点设置context
                child = await i.simple_fill(schema=schema, mode=mode)
                child_context = child.content  # 将返回内容（child.content）作为下一个子节点的context

            self.content = child_context  # 最后一个子节点返回的内容设置为父节点返回内容（self.content）
            return self


class WriteStoryStruct(Action):
    language: str = "Chinese"

    def __init__(self, name: str = "", language: str = "Chinese", *args, **kwargs):
        super().__init__()
        self.language = language
        self.node = OUTLINE_WRITE_NODES() if hasattr(OUTLINE_WRITE_NODES(), 'fill') else None  ## 包裹住ActionNodes

    async def run(self, topic: str, *args, **kwargs) -> Dict:
        DIRECTORY_PROMPT = """
        小说的题材是{topic}。请严格使用{language}语言，并按照instruction中的说明内容输出内容
        """

        # 我们设置好prompt，作为ActionNode的输入
        prompt = DIRECTORY_PROMPT.format(topic=topic, language=self.language)
        # 该方法会返回self，也就是一个ActionNode对象
        resp_node = await self.node.fill(context=prompt, llm=self.llm, schema="raw")
        # 选取ActionNode.content，获得我们期望的返回信息
        resp = resp_node.content
        # return Message(content=resp) # 返回Message对象后，会在resp前面加上 user: 字样，会破坏Dict结构
        return OutputParser.extract_struct(resp, dict)


class WriteContent(Action):
    language: str = "Chinese"
    directory: str = ""
    content: str = ""

    def __init__(self, name: str = "", directory: str = "", content: str = "", language: str = "Chinese", *args,
                 **kwargs):
        super().__init__()
        self.language = language
        self.directory = directory
        self.content = content
        self.node = CONTENT_WRITE_NODES() if hasattr(CONTENT_WRITE_NODES(), 'fill') else None

    async def run(self, topic: str, *args, **kwargs) -> str:
        CONTENT_PROMPT = """
        您现在是短篇小说领域经验丰富的小说作家。请以引人入胜的风格，深入细致地按照故事概况"{content}"写出故事内容，注意与上一章故事内容的衔接。
        上一章故事内容为{topic}。
        """
        n = 0
        while n < 5:
            prompt = CONTENT_PROMPT.format(
                topic=topic, language=self.language, content=self.content)
            resp_node = await self.node.fill(context=prompt, schema="raw", llm=self.llm)
            resp = resp_node.content

            n += 1
            if (resp.find("无法满足") == -1 or resp.find("无法完成") == -1):
                break
        resp = f"## {self.directory}\n\n{resp}"  ## 返回时要加上标题
        return resp


class StoryAssistant(Role):
    topic: str = ""
    total_content: str = ""
    language: str = "Chinese"
    main_title: str = ""

    def __init__(
            self,
            name: str = "Story Assistant",
            profile: str = "Story Assistant",
            language: str = "Chinese",
    ):
        super().__init__()
        self._init_actions([WriteStoryStruct(language=language)])  ## 先加入WriteStoryStruct Action
        self.language = language

    def _init_actions(self, actions: List[Action]) -> None:
        self.actions = actions


    async def _think(self) -> None:
        self.states = self.actions
        """Determine the next action to be taken by the role."""
        logger.info(self.rc.state)
        if self.rc.todo is None:
            self._set_state(0)
            return

        if self.rc.state + 1 < len(self.states):
            self._set_state(self.rc.state + 1)
        else:
            self.rc.todo = None

    async def _handle_directory(self, titles: Dict) -> Message:
        self.main_title = titles.get("title")
        directory = f"{self.main_title}\n"
        self.total_content += f"# {self.main_title}"
        actions = list()
        total_dir = titles.get("directory")
        for first_dir in total_dir:
            directory += f"- {first_dir}\n"
            dir_content = total_dir[first_dir]
            actions.append(WriteContent(
                language=self.language, directory=first_dir, content=dir_content))  ## 根据每章节目录，生成一系列WriteContent Action

        self._init_actions(actions)
        self.rc.todo = None
        return Message(content=directory)

    content_count: int = 0

    async def _act(self) -> Message:
        """Perform an action as determined by the role.

        Returns:
            A message containing the result of the action.
        """
        time.sleep(20)
        todo = self.rc.todo
        if type(todo) is WriteStoryStruct:
            msg = self.rc.memory.get(k=1)[0]
            self.topic = msg.content
            resp = await todo.run(topic=self.topic)
            logger.info(resp)
            return await self._handle_directory(resp)
        self.content_count += 1
        msg = self.rc.memory.get(k=1)[0]  ## 获取上章的内容，为prompt提供上下文信息，使章节之间的故事有连贯性
        if self.content_count > 1:
            if len(msg.content) > 100:
                resp = await todo.run(topic=msg.content[-100])  ## 我这里取的是上章内容最后100个字符
            else:
                resp = await todo.run(topic=msg.content)
        else:
            resp = await todo.run(topic=self.main_title)  ## 第一章内容生成之前，没有上文，所以这里传个小说题目进去，当作上下文

        if self.total_content != "":
            self.total_content += "\n\n\n"
        self.total_content += resp  ## 组装结果
        msg_last = Message(content=str(resp), role=self.profile)
        self.rc.memory.add(msg_last)  ## 将本章内容加入记忆中，以便下章作为上下文使用，可以在一定程度上保证上下文连贯性
        return msg_last

    async def _react(self) -> Message:
        while True:
            await self._think()
            if self.rc.todo is None:
                break
            msg = await self._act()
        root_path = TUTORIAL_PATH / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        logger.info(f"Write tutorial to {root_path}")
        await File.write(root_path, f"{self.main_title}.md", self.total_content.encode('utf-8'))
        return msg

    async def _write_to_file(self):
        root_path = TUTORIAL_PATH / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        logger.info(f"Write tutorial to {root_path}")
        await File.write(root_path, f"{self.main_title}.md", self.total_content.encode('utf-8'))


async def main():
    msg = "游戏"
    role = StoryAssistant()
    logger.info(msg)
    result = await role.run(msg)
    logger.info(result)

asyncio.run(main())

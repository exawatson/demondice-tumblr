import collections
import contextlib
import datetime
import gzip
import hashlib
import html
import itertools
import json
import logging
import math
import pathlib
import re
import shutil
import tempfile
import time
import urllib.parse
from typing import List, Literal, Optional, TypedDict

import httpx
import jinja2
import lxml.html
import orjson
import tenacity
import trio

logger = logging.getLogger(__name__)


class Tumbex:
    def __init__(self):
        self._client = None
        self._csrf_token: Optional[str] = None
        self._bearer_token: Optional[str] = None
        self._last_authorization: float = -math.inf
        self._authorization_lock: trio.Lock = trio.Lock()

    async def __aenter__(self, *args, **kwargs):
        self._client = await httpx.AsyncClient(
            timeout=120,
            limits=httpx.Limits(max_connections=20),
        ).__aenter__(*args, **kwargs)
        return self

    async def __aexit__(self, *args, **kwargs):
        if self._client is not None:
            await self._client.__aexit__(*args, **kwargs)
            self._client = None

    async def authorize(self) -> None:
        assert self._client
        response = await self._client.get("https://www.tumbex.com/")
        doc = lxml.html.fromstring(response.text)
        csrf_token = doc.cssselect('input[name="_csrf_token"]')[0].get("value")
        match = re.search(
            r'"{}", "([0-9a-f]{{64}})"'.format(re.escape(csrf_token)), response.text
        )
        assert match
        bearer_token = match.group(1)
        self._client.headers.update(
            {
                "x-csrf-token": csrf_token,
                "authorization": f"Bearer {bearer_token}",
                "x-requested-with": "XMLHttpRequest",
            }
        )
        self._last_authorization = time.monotonic()

    async def _check_authorization(self) -> None:
        async with self._authorization_lock:
            if (time.monotonic() - self._last_authorization) > datetime.timedelta(
                minutes=30
            ).total_seconds():
                await self.authorize()

    async def _http_get(self, *args, **kwargs):
        assert self._client
        await self._check_authorization()
        response = await self._client.get(*args, **kwargs)
        response.raise_for_status()
        response_json = response.json()
        response.status_code = response_json["meta"]["code"]
        response.extensions["reason_phrase"] = response_json["meta"]["status"].encode(
            "utf-8"
        )
        if response.status_code == 403:
            self._last_authorization = -math.inf
        response.raise_for_status()
        return response_json["response"]

    @tenacity.retry(stop=tenacity.stop_after_attempt(3), sleep=trio.sleep, reraise=True)
    async def get_posts(
        self, username: str, page: int, type: str = "posts", tag: str = ""
    ):
        response = await self._http_get(
            "https://api.1.tumbex.com/api/tumblr/posts",
            params={
                "tumblr": username,
                "type": type,
                "page": page,
                "tag": tag,
            },
        )
        return response

    async def _get_all_posts_page(self, send_channel, *args, **kwargs):
        async with send_channel:
            try:
                response = await self.get_posts(*args, **kwargs)
            except httpx.HTTPStatusError as ex:
                if ex.response.status_code == 401:
                    raise
                logger.warning(
                    "Exhausted retries for get_posts(*%r, **%r), assuming broken",
                    args,
                    kwargs,
                )
                return
            for post in response.get("posts", []):
                await send_channel.send(post)

    async def get_all_posts(self, username, type: str = "posts", tag: str = ""):
        try:
            response = await self.get_posts(
                username=username, page=0, type=type, tag=tag
            )
        except httpx.HTTPStatusError as ex:
            if ex.response.status_code == 404:
                logger.info("User does not exist: %r", username)
                # user does not exist, just return empty
                return
            raise
        posts_per_page = 20
        total_posts = response.get("total", 0)
        page_count = total_posts // posts_per_page + bool(total_posts % posts_per_page)
        logger.info("Downloading %d posts across %d pages", total_posts, page_count)
        async with trio.open_nursery() as nursery:
            send_channel, receive_channel = trio.open_memory_channel(0)
            async with send_channel:
                for page in range(page_count):
                    nursery.start_soon(
                        self._get_all_posts_page,
                        send_channel.clone(),
                        username,
                        page,
                        type,
                        tag,
                    )
            async with receive_channel:
                post_count = 0
                async for value in receive_channel:
                    yield value
                    post_count += 1
                    if not (post_count % 200):
                        logger.info("Tasks in nursery: %d", len(nursery.child_tasks))

    async def get_post(self, username, id: int):
        response = await self._http_get(
            "https://api.1.tumbex.com/api/tumblr/post",
            params={
                "tumblr": username,
                "id": id,
            },
        )
        return response["posts"][0]


BASE_PATH = pathlib.Path(__file__).parent
USERS_PATH = BASE_PATH / "users"
POSTS_PATH = BASE_PATH / "posts"
REBLOGS_PATH = BASE_PATH / "reblogs.txt"
EXTRACTED_PATH = BASE_PATH / "extracted.jsonl"
RENDERED_PATH = BASE_PATH / "index.html"
TARGET_USER = "demondice-deactivated20200831"
BOOTSTRAP_USERS = {
    "thezombiedogz",
    "brutalfish",
    "biteghost",
    "0tacoon",
    "raebird5",
    "witchplastic",
}


def extract_rebloggers():
    users = set(BOOTSTRAP_USERS)
    for entry in POSTS_PATH.glob("**/*.json"):
        with entry.open("r") as f:
            doc = orjson.loads(f.read())
        users.add(doc["tumblr"])
        for note in doc.get("notes", []):
            if note["type"] == "reblog" and "reblog" in note:
                users.add(note["reblog"])
            users.add(note["tumblr"])
        for block in doc["blocks"]:
            users.add(block["blog"]["name"])
    with REBLOGS_PATH.open("w") as f:
        for user in sorted(users):
            f.write(user + "\n")


def load_rebloggers():
    users = set(BOOTSTRAP_USERS)
    with REBLOGS_PATH.open("r") as f:
        for line in f:
            users.add(line.strip())
    return users


async def download_blog(tumbex, user):
    logger.info("Downloading user %r", user)
    destination = USERS_PATH / f"{user}.jsonl.gz"
    if not destination.exists():
        with tempfile.TemporaryDirectory() as tempdir:
            temp_output = pathlib.Path(tempdir) / destination.name
            with gzip.open(temp_output, "wb") as f:
                async for post in tumbex.get_all_posts(user):
                    f.write(orjson.dumps(post) + b"\n")
            shutil.move(temp_output, destination)
    else:
        logger.info("User %r already cached, skipping", user)


async def download_post(tumbex, user, post_id):
    logger.info("Downloading user %r post %r", user, post_id)
    write_post(await tumbex.get_post(user, post_id))


def write_post(post):
    destination = POSTS_PATH / post["tumblr"] / f"{post['id']}.json"
    if not destination.exists():
        with tempfile.TemporaryDirectory() as tempdir:
            temp_output = pathlib.Path(tempdir) / destination.name
            with temp_output.open("wb") as f:
                f.write(orjson.dumps(post))
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(temp_output, destination)
    else:
        logger.info("Post %r already cached, skipping", post["id"])


def generate_master_post_list():
    with EXTRACTED_PATH.open("wb") as f_output:
        for entry in POSTS_PATH.glob("**/*.json"):
            with entry.open("r") as f_input:
                doc = orjson.loads(f_input.read())
            f_output.write(orjson.dumps(doc) + b"\n")


def extract_video_id(link):
    return urllib.parse.parse_qs(urllib.parse.urlparse(link).query)["v"][0]


def extract_video_title(html):
    return lxml.html.fromstring(html).get("title")


def clean_url(url: str) -> str:
    if url.startswith("https://href.li/?"):
        return url[len("https://href.li/?") :]
    return url


FORMATTING_DIRECTIVE_TYPE_TO_TAG = {
    "link": "a",
    "mention": "a",
    "strikethrough": "s",
    "italic": "i",
    "bold": "b",
    "small": "small",
}


def apply_formatting(text: str, directives: List[dict]) -> str:
    """
    hahaha there is a 0% chance this is correct in all situations and it's ugly as fuck
    but it seems to work well enough on this data
    """
    buffer = []
    directives.sort(key=lambda d: (d["start"], -d["end"], d["type"]))
    stack = []
    for idx, c in enumerate(text):
        for directive in directives:
            if idx == directive["start"]:
                element_attrs = {}
                if directive["type"] == "link":
                    element_attrs["href"] = clean_url(directive["url"])
                elif directive["type"] == "mention":
                    element_attrs["href"] = directive["blog"]["url"]
                element_attrs_rendered = " ".join(
                    f'{k}="{html.escape(v)}"' for k, v in element_attrs.items()
                )
                if element_attrs_rendered:
                    element_attrs_rendered = f" {element_attrs_rendered}"
                buffer.append(
                    f"<{FORMATTING_DIRECTIVE_TYPE_TO_TAG[directive['type']]}{element_attrs_rendered}>"
                )
                stack.append(directive)
        buffer.append(html.escape(c))
        while stack and stack[-1]["end"] == idx + 1:
            directive = stack.pop()
            buffer.append(f"</{FORMATTING_DIRECTIVE_TYPE_TO_TAG[directive['type']]}>")
    assert not stack
    return "".join(buffer)


def render_html():
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(BASE_PATH),
        autoescape=jinja2.select_autoescape(),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )

    with EXTRACTED_PATH.open("r") as input:
        posts_by_parent = collections.defaultdict(list)
        for post in (orjson.loads(line) for line in input):
            posts_by_parent[int(post["reblogRoot"]["id"])].append(post)
        posts = [
            posts[0]["blocks"][0]
            for _, posts in sorted(posts_by_parent.items(), reverse=True)
        ]

    with RENDERED_PATH.open("w") as output:
        output.write(
            env.get_template("template.html").render(
                posts=posts,
                extract_video_id=extract_video_id,
                extract_video_title=extract_video_title,
                apply_formatting=apply_formatting,
            )
        )


def load_archived_user_posts(username):
    with gzip.open(USERS_PATH / (username + ".jsonl.gz"), "rb") as f:
        for post in (orjson.loads(line) for line in f):
            yield post


def load_archived_users():
    for entry in USERS_PATH.glob("*.jsonl.gz"):
        username = entry.stem.rsplit(".", 1)[0]
        logger.info("Loading saved posts from user %r", username)
        yield username, load_archived_user_posts(username)


def is_relevant_post(post):
    return "reblogRoot" in post and post["reblogRoot"]["name"] == TARGET_USER


def extracted_posts_exist_for_user(username):
    if (POSTS_PATH / username / ".done").exists():
        logger.info("Already extracted posts from this user")
        return True
    return False


def mark_user_as_extracted(username):
    (POSTS_PATH / username / ".done").mkdir(parents=True, exist_ok=True)


async def extract_relevant_posts(tumbex):
    for username, posts in load_archived_users():
        if extracted_posts_exist_for_user(username):
            continue
        for post in filter(is_relevant_post, posts):
            try:
                await download_post(tumbex, post["tumblr"], post["id"])
            except httpx.HTTPStatusError as ex:
                if ex.response.status_code == 404:
                    logger.info("Got 404 on post detail, writing simple version")
                    write_post(post)
                else:
                    raise
        mark_user_as_extracted(username)


async def async_main():
    logging.basicConfig(level=logging.INFO)

    for path in (BASE_PATH, USERS_PATH, POSTS_PATH):
        path.mkdir(parents=True, exist_ok=True)

    async with Tumbex() as tumbex:
        await extract_relevant_posts(tumbex)
        generate_master_post_list()
        extract_rebloggers()

        render_html()

        user_blogs = load_rebloggers()
        logger.info("Loaded %d blogs", len(user_blogs))
        for user in user_blogs:
            await download_blog(tumbex, user)


def main():
    trio.run(async_main)


if __name__ == "__main__":
    main()

import asyncio
import json
import os
import sys
from typing import Optional
import tempfile
import argparse
import shlex
import random
import sqlite3
from collections import namedtuple
from contextlib import asynccontextmanager
import jinja2
from jinja2 import Environment, select_autoescape


from nio import (
    AsyncClient,
    ClientConfig,
    DevicesError,
    Event,
    InviteEvent,
    LocalProtocolError,
    LoginResponse,
    MatrixRoom,
    MatrixUser,
    RoomMessageText,
    RoomSendResponse,
    KeyVerificationEvent,
    ToDeviceEvent,
    KeyVerificationStart,
    RedactionEvent,
    crypto,
    exceptions,
    RoomEncryptedAudio,
    DownloadResponse,
    KeyVerificationAccept,
    KeyVerificationKey,
)
import nio

SCORING_TABLE_PREFIXES = [
    (1200, [1, 2, 3, 4, 5, 6]),
    ( 800, [2, 2, 3, 3, 4, 4]),
    (8000, [1, 1, 1, 1, 1, 1]),
    (4000, [1, 1, 1, 1, 1]),
    (2000, [1, 1, 1, 1]),
    (1000, [1, 1, 1]),
    ( 100, [1]),
    (1600, [2, 2, 2, 2, 2, 2]),
    ( 800, [2, 2, 2, 2, 2]),
    ( 400, [2, 2, 2, 2]),
    ( 200, [2, 2, 2]),
    (2400, [3, 3, 3, 3, 3, 3]),
    (1200, [3, 3, 3, 3, 3]),
    ( 600, [3, 3, 3, 3]),
    ( 300, [3, 3, 3]),
    (3200, [4, 4, 4, 4, 4, 4]),
    (1600, [4, 4, 4, 4, 4]),
    ( 800, [4, 4, 4, 4]),
    ( 400, [4, 4, 4]),
    (4000, [5, 5, 5, 5, 5, 5]),
    (2000, [5, 5, 5, 5, 5]),
    (1000, [5, 5, 5, 5]),
    ( 500, [5, 5, 5]),
    (  50, [5]),
    (4800, [6, 6, 6, 6, 6, 6]),
    (2400, [6, 6, 6, 6, 6]),
    (1200, [6, 6, 6, 6]),
    ( 600, [6, 6, 6]),
]

def greed_roll_score(roll):
    # score_acc = 0
    score_records = []
    while True:
        for vv in SCORING_TABLE_PREFIXES:
            (score, prefix) = vv
            if roll[:len(prefix)] == prefix:
                # score_acc += score
                score_records.append(vv)
                roll = roll[len(prefix):]
                break
        else:
            break
    return score_records
    # return score_acc

def greed_roll():
    rolls = []
    for i in range(6):
        rolls.append(random.choice(range(1, 7)))
    rolls.sort()
    return rolls


def greed_format_roll_and_score(roll):
    score_parts = greed_roll_score(roll)
    point_tot = sum(x for (x, _) in score_parts)
    score_summary = ", ".join(f"[{', '.join(map(str, prefix))} => {score}]" for (score, prefix) in score_parts)
    return f"{roll!r} => [{score_summary}] for {point_tot}"


class CommandGeneralFailure(RuntimeError):
    pass


class CommandGeneralFailureWorst(RuntimeError):
    pass


class MatrixArgumentParserArgumentError(RuntimeError):
    pass


class MatrixArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
    def print_help(self, status=0, message=None):
        raise MatrixArgumentParserArgumentError(super().format_help())
    def exit(self, status=0, message=None):
        print(f"{status=!r}, {message=!r}")
        if message is None:
            return
        raise MatrixArgumentParserArgumentError(message)


jinja_env = Environment(
    loader=jinja2.PackageLoader("nano"),
    autoescape=select_autoescape(
        enabled_extensions=('html', 'xml', 'html.jinja', 'xml.jinja'),
        default_for_string=True,
    )
)

class greed_score(namedtuple('_greed_score', ['rolling_user_id', 'score_sum', 'game_count', 'win_count'])):
    def to_matrix_message(self):
        return {
            "msgtype": "m.text",
            "body": f"scored {self.score_sum} across {self.game_count} games ({self.win_count} won)"
        }

class greed_score_view_model(namedtuple('_greed_score', ['rolling_user', 'score_sum', 'game_count', 'win_count'])):
    @classmethod
    def from_greed_score(cls, gs: greed_score, room: MatrixRoom):
        return cls(
            rolling_user=room.user_name(gs.rolling_user_id),
            score_sum=gs.score_sum,
            game_count=gs.game_count,
            win_count=gs.win_count
        )


class greed_score_multi(namedtuple('_greed_score_multi', ['items'])):
    def to_matrix_message(self, room):
        leaderboard = greed_score_multi([greed_score_view_model.from_greed_score(gs, room) for gs in self.items])
        plaintext = jinja_env.get_template("greed/leaderboard.txt.jinja").render(leaderboard=leaderboard)
        richtext = jinja_env.get_template("greed/leaderboard.html.jinja").render(leaderboard=leaderboard)
        return {
            "msgtype": "m.text",
            "body": plaintext,
            "format": "org.matrix.custom.html",
            "formatted_body": richtext,
        }


class GreedEngine(object):
    def __init__(self, database_path):
        self._database_path = database_path

    def score_leaderboard(self, channel_id, limit=5):
        if limit is None:
            limit = 5
        con = sqlite3.connect(self._database_path)
        cur = con.cursor()
        cur.execute("""
            SELECT
                rolling_user_id,
                SUM(final_score) AS score_sum,
                COUNT(1) AS game_count,
                SUM(final_score > 0) AS games_won_count
            FROM greed_games
            WHERE channel_id = :channel_id AND matched_event_id IS NOT NULL
            GROUP BY rolling_user_id
            ORDER BY score_sum DESC
            LIMIT :limit
        """, {
            'channel_id': channel_id,
            'limit': limit,
        })
        return greed_score_multi([greed_score(*x) for x in cur.fetchall()])

    def sum_score(self, channel_id, rolling_user_id):
        con = sqlite3.connect(self._database_path)
        # "/mnt/ceph-transmission/nano/greed.sqlite")
        cur = con.cursor()
        cur.execute("""
            SELECT
                rolling_user_id,
                SUM(final_score) AS score_sum,
                COUNT(1) AS game_count,
                SUM(final_score > 0) AS games_won_count
            FROM greed_games
            WHERE channel_id = :channel_id
            AND rolling_user_id = :rolling_user_id
            AND matched_event_id IS NOT NULL
        """, {
            'rolling_user_id': rolling_user_id,
            'channel_id': channel_id,
        })
        return greed_score(*cur.fetchone())

    def play_apply(self, event_id, channel_id, rolling_user_id):
        con = sqlite3.connect(self._database_path)
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS
            greed_games(
                event_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                matched_event_id TEXT,
                rolling_user_id TEXT NOT NULL,
                roll TEXT NOT NULL,
                final_score INT
            )
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
            greed_games_unmatched ON greed_games(channel_id)
            WHERE matched_event_id IS NULL
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
            greed_games_event_id ON greed_games(event_id)
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
            greed_games_matched_event_id ON greed_games(matched_event_id)
            WHERE matched_event_id IS NOT NULL
        """)
        con.commit()

        cur = con.cursor()
        cur.execute("""
            SELECT event_id, rolling_user_id, roll FROM greed_games
            WHERE channel_id = :channel_id AND matched_event_id IS NULL
            LIMIT 2
        """, {
            'channel_id': channel_id,
        })

        roll = greed_roll()
        records = cur.fetchall()
        if not records:
            # new game
            cur.execute("""
                INSERT INTO greed_games
                (event_id, channel_id, rolling_user_id, roll)
                VALUES (:event_id, :channel_id, :rolling_user_id, :roll)
            """, {
                'event_id': event_id,
                'channel_id': channel_id,
                'rolling_user_id': rolling_user_id,
                'roll': json.dumps(roll),
            })
            con.commit()
            return
        elif len(records) != 1:
            # game engine broken
            raise CommandGeneralFailureWorst("game engine broken")
        else:
            # play against current
            (record, ) = records
            (foreign_event_id, foreign_rolling_user_id, foreign_roll) = record
            foreign_roll = json.loads(foreign_roll)
            if foreign_rolling_user_id == rolling_user_id:
                raise CommandGeneralFailure("can't roll against yourself")

            foreign_score_abs = sum(x for (x, _) in greed_roll_score(foreign_roll))
            local_score_abs = sum(x for (x, _) in greed_roll_score(roll))
            cur.execute("""
                UPDATE greed_games
                SET matched_event_id = :event_id,
                    final_score = :final_score
                WHERE channel_id = :channel_id AND matched_event_id IS NULL
            """, {
                'event_id': event_id,
                'final_score': foreign_score_abs - local_score_abs,
                'channel_id': channel_id,
            })
            cur.execute("""
                INSERT INTO greed_games
                (event_id, channel_id, matched_event_id, rolling_user_id, roll, final_score)
                VALUES (:event_id, :channel_id, :matched_event_id, :rolling_user_id, :roll, :final_score)
            """, {
                'event_id': event_id,
                'channel_id': channel_id,
                'matched_event_id': foreign_event_id,
                'rolling_user_id': rolling_user_id,
                'roll': json.dumps(roll),
                'final_score': local_score_abs - foreign_score_abs,
            })
            con.commit()
            return {
                'channel_id': channel_id,
                'matched_event_id': foreign_event_id,
                'foreign_roll': foreign_roll,
                'foreign_rolling_user_id': foreign_rolling_user_id,
                'foreign_score_val': foreign_score_abs - local_score_abs,
                'local_rolling_user_id': rolling_user_id,
                # 'local_score_abs': local_score_abs,
                'local_score_val': local_score_abs - foreign_score_abs,
                'local_roll': roll,
            }


commands = {}


def generate_arg_parser_llama():
    parser = MatrixArgumentParser(prog='!llama')
    parser.add_argument('--char-profile', default='assistant', help='The profile to load')
    parser.add_argument('--char-name', help='Use this character name instead of the bot\'s name')
    parser.add_argument('--user-name', help='Use this user name instead of the user\'s display name')
    parser.add_argument('--prompt', required=True, help='The starting prompt to use')
    return parser


LLAMA_ARG_PARSER = generate_arg_parser_llama()


async def handle_command_llama(client, room: MatrixRoom, event: RoomMessageText, args):
    try:
        with open(f'{event.event_id}.bot-meta', 'r') as fh:
            print("x")
            return
    except FileNotFoundError:
        pass
    with open(f'{event.event_id}.bot-meta', 'w') as fh:
        json.dump({'command': '!llama'}, fh)
    try:
        async for e in start_thread_continuation(client, room, event, event.event_id, args):
            if e is EVENT_STARTED:
                await client.room_send_unver(room.room_id, "m.reaction", {
                    "m.relates_to": {
                        "rel_type": "m.annotation",
                        "event_id": event.event_id,
                        "key": "🤔",
                    }
                })
            if isinstance(e, EventCompleted):
                await client.room_send_unver(room.room_id, "m.reaction", {
                    "m.relates_to": {
                        "rel_type": "m.annotation",
                        "event_id": event.event_id,
                        "key": "✅",
                    }
                })
    except RuntimeError as e:
        print(f"error: {e}")
        return await client.room_send_unver(room.room_id, "m.reaction", {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": event.event_id,
                "key": "❌",
            }
        })


async def handle_command_llama_thread_continue(client, room: MatrixRoom, event: RoomMessageText, event_thread_id: str):
    try:
        async for e in run_thread_continuation(client, room, event, event_thread_id):
            if e is EVENT_STARTED:
                await client.room_send_unver(room.room_id, "m.reaction", {
                    "m.relates_to": {
                        "rel_type": "m.annotation",
                        "event_id": event.event_id,
                        "key": "🤔",
                    }
                })
            if isinstance(e, EventCompleted):
                await client.room_send_unver(room.room_id, "m.reaction", {
                    "m.relates_to": {
                        "rel_type": "m.annotation",
                        "event_id": event.event_id,
                        "key": "✅",
                    }
                })
    except RuntimeError:
        return await client.room_send_unver(room.room_id, "m.reaction", {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": event.event_id,
                "key": "❌",
            }
        })


commands['!llama'] = {
    'parser': LLAMA_ARG_PARSER,
    'handler': handle_command_llama,
    'requires_unthreaded': (True,),
    'thread_continuation': handle_command_llama_thread_continue,
}

def generate_arg_parser_greed():
    parser = MatrixArgumentParser(prog='!greed')
    subparsers = parser.add_subparsers(title='subcommands', description='valid subcommands', dest='subparser_name')
    parser_stats = subparsers.add_parser('stats', help='print your stats')
    parser_stats.add_argument('--channel-id', default=None, help='The channel ID to run stats on')
    parser_stats.add_argument('--user-id', default=None, help='The user ID to run stats on')
    parser_leader = subparsers.add_parser('leaderboard', help='print the leaderboard')
    parser_leader.add_argument('--channel-id', default=None, help='The channel ID to run stats on')
    parser_leader.add_argument('--limit', default=5, type=int)
    parser_leader.add_argument('--plain-text', action='store_true')
    return parser


GREED_ARG_PARSER = generate_arg_parser_greed()


async def handle_command_greed(client, room: MatrixRoom, event: RoomMessageText, args):
    engine = GreedEngine(client._bot_config['greed-database'])
    target_channel = room
    if args.subparser_name in ('stats', 'leaderboard') and args.channel_id is not None:
        target_channel = client.rooms[args.channel_id]
    target_user_id = event.sender
    if args.subparser_name in ('stats',) and args.user_id is not None:
        target_user_id = args.user_id

    if args.subparser_name == 'stats':
        greed_sum = None
        async with call_command(client, room.room_id, event.event_id):
            greed_sum = engine.sum_score(target_channel.room_id, target_user_id)
        if greed_sum:
            return await client.room_send_unver(room.room_id, "m.room.message", greed_sum.to_matrix_message())
    elif args.subparser_name == 'leaderboard':
        greederboard = None
        async with call_command(client, room.room_id, event.event_id):
            greederboard = engine.score_leaderboard(target_channel.room_id, args.limit)
        if greederboard:
            message = greederboard.to_matrix_message(target_channel)
            if args.plain_text:
                message.pop("format")
                message.pop("formatted_body")
            return await client.room_send_unver(room.room_id, "m.room.message", message)
    elif args.subparser_name is None:
        play_result = None
        async with call_command(client, room.room_id, event.event_id, announce_success=True):
            play_result = engine.play_apply(event.event_id, target_channel.room_id, event.sender)
        if play_result is None:
            return

        # foreign_score_parts = greed_roll_score(play_result['foreign_roll'])
        # local_score_parts = greed_roll_score(play_result['local_roll'])
        f_status = greed_format_roll_and_score(play_result['foreign_roll'])
        l_status = greed_format_roll_and_score(play_result['local_roll'])

        # foreign_score_abs = sum(x for (x, _) in greed_roll_score(play_result['local_roll']))
        # local_score_abs = sum(x for (x, _) in greed_roll_score(roll))
        # greed_format_roll_and_score()
        # aibi (-300): [1, 1, 2, 2, 2, 6] => [[1 => 100], [1 => 100], [2, 2, 2 => 200]] for 400 points
        prev_player = room.user_name(play_result['foreign_rolling_user_id'])
        cur_player = room.user_name(event.sender)
        return await client.room_send_unver(room.room_id, "m.room.message", {
            "msgtype": "m.text",
            "body": "\n".join([
                f"{prev_player}: ({play_result['foreign_score_val']}) {f_status}",
                f"{cur_player}: ({play_result['local_score_val']}) {l_status}",
            ]),
        })


commands['!greed'] = {
    'parser': GREED_ARG_PARSER,
    'handler': handle_command_greed,
}

def generate_arg_parser_ping():
    parser = MatrixArgumentParser(prog='!ping')
    return parser


PING_ARG_PARSER = generate_arg_parser_ping()


async def handle_command_ping(client, room: MatrixRoom, event: RoomMessageText, args):
    try:
        return await client.room_send_unver(room.room_id, "m.reaction", {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": event.event_id,
                "key": "✅",
            }
        })
    except exceptions.OlmUnverifiedDeviceError as err:
        print(f"{err=!r}")
        print("These are all known devices:")
        device_store: crypto.DeviceStore = self.device_store
        [
            print(
                f"\t{device.user_id}\t {device.device_id}\t {device.trust_state}\t  {device.display_name}"
            )
            for device in device_store
        ]


commands['!ping'] = {
    'parser': PING_ARG_PARSER,
    'handler': handle_command_ping,
}


"""
if cmd_lexed and cmd_lexed[0] == '!ping-back' and event_thread_id is None:
            parser = MatrixArgumentParser(prog='!ping-back')
            try:
                args = parser.parse_args(cmd_lexed[1:])
            except MatrixArgumentParserArgumentError as e:
                doc = {
                    "msgtype": "m.message",
                    "body": str(e),
                }
                doc['m.relates_to'] = {
                    'rel_type': 'm.thread',
                    'event_id': event.event_id,
                }
                return await self.room_send_unver(room.room_id, "m.room.message", doc)
            doc = {
                "msgtype": "m.text",
                "body": f"{room.user_name(event.sender)}: ping!",
                #"format": "org.matrix.custom.html",
                #"formatted_body": f"<a href=\"https://matrix.to/#/{event.sender}\">{room.user_name(event.sender)}</a>: ping!",
                'm.relates_to': {
                    'rel_type': 'm.thread',
                    'event_id': event.event_id,
                }
            }
            return await self.room_send_unver(room.room_id, "m.room.message", doc)
"""

@asynccontextmanager
async def call_command(client, room_id, event_id, announce_success=False):
    try:
        yield
    except CommandGeneralFailure:
        await client.room_send_unver(room_id, "m.reaction", {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": event_id,
                "key": "❌",
            }
        })
        return
    except CommandGeneralFailureWorst:
        await client.room_send_unver(room_id, "m.reaction", {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": event_id,
                "key": "⚠️",
            }
        })
        await client.room_send_unver(room_id, "m.reaction", {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": event_id,
                "key": "❌",
            }
        })
        return
    if announce_success:
        await client.room_send_unver(room_id, "m.reaction", {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": event_id,
                "key": "✅",
            }
        })


# import logging
# logging.basicConfig(level=logging.DEBUG)

class ExecutionError(RuntimeError):
    pass

ROOM_ID = '!hjeQxmxYoiYGGtAzuc:m-test.yyc1.yshi.org'

EVENT_STARTED = object()

class EventCompleted(object):
    def __init__(self, value=None):
        self.value = value

# LLM_MODEL = 'llama-2-7b.Q5_K_M.gguf'
# LLM_MODEL = 'llama2_70b_chat_uncensored.Q4_K_M.gguf'
LLM_MODEL = 'mythomax-l2-kimiko-v2-13b.Q5_K_M.gguf'

def get_event_thread_id(event):
    rel_to = event.source.get('content', {}).get('m.relates_to', {})
    rel_type = rel_to.get('rel_type', None)
    if rel_type != 'm.thread':
        return None
    return rel_to.get('event_id', None)


async def start_thread_continuation(client, room, event, event_thread_id, args):
    print(f"client={client.__dict__=!r}")
    print(f"args={args.__dict__=!r}")
    filename = f'{event_thread_id}.log'
    print(f"\n\nstarting conversation {filename!r}\n")
    if os.path.exists(filename):
        return
    char_name = args.char_name
    message_body_text = args.prompt
    if char_name is None:
        char_name = room.user_name(client.user_id)
    user_name = args.user_name
    if user_name is None:
        user_name = room.user_name(event.sender)
    character_template = None
    try:
        characters = os.listdir('characters')
        characters = [x for x in characters if x == f"character-{args.char_profile}.template"]
        if characters:
            with open(os.path.join('characters', characters[0]), 'r') as cht:
                character_template = cht.read().replace('{{char}}', char_name).replace('{{user}}', user_name)
    except FileNotFoundError:
        pass
    if character_template is None:
        raise RuntimeError("no character template")
    with open(filename, 'a') as fh:
        fh.write(character_template)
        print(f"\n{user_name}: {message_body_text}", file=fh)
        print(f"{char_name}: ", end='', file=fh)
        offset = fh.tell()
    with open(f"{event_thread_id}.meta", 'w') as ofh:
        json.dump({'char_name': char_name, 'user_name': user_name}, ofh)
    with open(f"{event_thread_id}.output", 'w+') as ofh:
        ofh.seek(0)
        proc = await asyncio.create_subprocess_exec(
            'llama',
            '--prompt-cache-all', '--prompt-cache', f"{event_thread_id}.pcache",
            '--model', f'/mnt/ceph-transmission/models/llm/{LLM_MODEL}',
            '--log-disable', '-c', '4096', '--temp', '0.7', '--repeat_penalty', '1.1', '-n', '-1',
            '--simple-io', '--reverse-prompt', f"{user_name}:",
            '-f', filename,
            stdin=asyncio.subprocess.DEVNULL,
            # stderr=asyncio.subprocess.DEVNULL,
            stdout=ofh)
    yield EVENT_STARTED
    await proc.wait()
    stdout_parts = []
    with open(f"{event_thread_id}.output", 'r') as ofh:
        ofh.seek(offset)
        for line in ofh:
            if line.startswith(f"{user_name}:"):
                break
            stdout_parts.append(line)
    stdout = ''.join(stdout_parts)
    yield EventCompleted()
    with open(filename, 'a') as fh:
        fh.write(stdout)
    await client.room_send(room.room_id, "m.room.message", {
        "msgtype": "m.message",
        "body": stdout.strip(),
        'm.relates_to': {
            'rel_type': 'm.thread',
            'event_id': event_thread_id,
        }
    })


async def run_thread_continuation(client, room, event, event_thread_id):
    filename = f'{event_thread_id}.log'
    if not os.path.exists(filename):
        return
    meta = {}
    try:
        with open(f'{event_thread_id}.meta') as fh:
            meta = json.load(fh)
    except FileNotFoundError:
        pass
    char_name = meta.get('char_name', None)
    if char_name is None:
        char_name = room.user_name(client.user_id)
    user_name = meta.get('user_name', None)
    if user_name is None:
        user_name = room.user_name(event.sender)
    with open(filename, 'a') as fh:
        print(f"\n{user_name}: {event.body}", file=fh)
        print(f"{char_name}: ", end='', file=fh)
        offset = fh.tell()
    with open(f"{event_thread_id}.output", 'w+') as ofh:
        ofh.seek(0)
        proc = await asyncio.create_subprocess_exec(
            'llama',
            '--prompt-cache-all', '--prompt-cache', f"{event_thread_id}.pcache",
            '--model', f'/mnt/ceph-transmission/models/llm/{LLM_MODEL}',
            '--log-disable', '-c', '4096', '--temp', '0.7', '--repeat_penalty', '1.1', '-n', '-1',
            '--simple-io', '--reverse-prompt', f"{user_name}:",
            '-f', filename,
            stdin=asyncio.subprocess.DEVNULL,
            # stderr=asyncio.subprocess.DEVNULL,
            stdout=ofh)
    yield EVENT_STARTED
    await proc.wait()
    stdout_parts = []
    with open(f"{event_thread_id}.output", 'r') as ofh:
        ofh.seek(offset)
        for line in ofh:
            if line.startswith(f"user_name:"):
                break
            stdout_parts.append(line)
    stdout = ''.join(stdout_parts)
    yield EventCompleted()
    with open(filename, 'a') as fh:
        fh.write(stdout)
    # os.rename(f"{event_thread_id}.output", f"{event_thread_id}.log")
    doc = {
        "msgtype": "m.message",
        "body": stdout.strip(),
    }
    doc['m.relates_to'] = {
        'rel_type': 'm.thread',
        'event_id': event_thread_id,
    }
    await client.room_send(room.room_id, "m.room.message", doc)


async def transcribe_audio_file(room: MatrixRoom, event: RoomEncryptedAudio, resp: DownloadResponse):
    if hasattr(event, "key"):
        decrypted_content = nio.crypto.decrypt_attachment(resp.body, **{
            "key": event.key["k"],
            "hash": event.hashes["sha256"],
            "iv": event.iv,
        })
    else:
        decrypted_content = resp.body
    with open(f"{event.event_id}.ogg", "wb") as f:
        f.write(decrypted_content)
    
    yield EVENT_STARTED
    proc = await asyncio.create_subprocess_exec(
        'ffmpeg',
        '-i', f"{event.event_id}.ogg",
        "-ar", "16k",
        f"{event.event_id}.wav",
        stdin=asyncio.subprocess.DEVNULL)
    await proc.wait()

    with open(f"{event.event_id}.output", "w") as ofh:

        proc = await asyncio.create_subprocess_exec(
            'whisper-cpp',
            '--model', f'/mnt/ceph-transmission/models/whisper.cpp/ggml-medium.bin',
            '--language', 'auto',
            f"{event.event_id}.wav",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=ofh)
        await proc.wait()
    with open(f"{event.event_id}.output", "r") as ofh:
        buf = ofh.read()
    yield EventCompleted(buf)


class CustomEncryptedClient(AsyncClient):
    def __init__(
        self,
        homeserver,
        user="",
        device_id="",
        store_path="",
        config=None,
        ssl=None,
        proxy=None,
        bot_config=None,
    ):
        # Calling super.__init__ means we're running the __init__ method
        # defined in AsyncClient, which this class derives from. That does a
        # bunch of setup for us automatically
        super().__init__(
            homeserver,
            user=user,
            device_id=device_id,
            store_path=store_path,
            config=config,
            ssl=ssl,
            proxy=proxy,
        )
        self._bot_config = bot_config

        # if the store location doesn't exist, we'll make it
        if store_path and not os.path.isdir(store_path):
            os.mkdir(store_path)

        # auto-join room invites
        self.add_event_callback(self.cb_autojoin_room, InviteEvent)

        # print all the messages we receive
        self.add_event_callback(self.cb_room_message, nio.events.room_events.Event)
        self.add_event_callback(self.cb_print_messages, RoomMessageText)
        self.add_event_callback(self.cb_audio_file, RoomEncryptedAudio)
        #self.add_event_callback(self.cb_redaction_event, RedactionEvent)
        #self.add_to_device_callback(self.cb_to_device_event, (ToDeviceEvent, ))
        #self.add_to_device_callback(self.cb_key_verify_event, (KeyVerificationEvent, ))
        # self.add_to_device_callback(self.cb_key_verify_start, (KeyVerificationStart, ))
        # self.add_to_device_callback(self.cb_key_verify_event, (KeyVerificationEvent, ))
        self.add_to_device_callback(self.cb_key_verify_key, (KeyVerificationKey, ))
        self.add_to_device_callback(self.cb_key_verification_accept, (KeyVerificationAccept, ))

    async def login(self) -> None:
        """Log in either using the global variables or (if possible) using the
        session details file.

        NOTE: This method kinda sucks. Don't use these kinds of global
        variables in your program; it would be much better to pass them
        around instead. They are only used here to minimise the size of the
        example.
        """
        # Restore the previous session if we can
        # See the "restore_login.py" example if you're not sure how this works
        if os.path.exists(self._bot_config['credentials-file']) and os.path.isfile(
            self._bot_config['credentials-file']
        ):
            try:
                with open(self._bot_config['credentials-file'], "r") as f:
                    config = json.load(f)
                    self.access_token = config["access_token"]
                    self.user_id = config["user_id"]
                    self.device_id = config["device_id"]

                    # This loads our verified/blacklisted devices and our keys
                    self.load_store()
                    print(
                        f"Logged in using stored credentials: {self.user_id} on {self.device_id}"
                    )

            except IOError as err:
                print(f"Couldn't load session from file. Logging in. Error: {err}")
            except json.JSONDecodeError:
                print("Couldn't read JSON file; overwriting")

        # We didn't restore a previous session, so we'll log in with a password
        if not self.user_id or not self.access_token or not self.device_id:
            # this calls the login method defined in AsyncClient from nio
            resp = await super().login(self._bot_config['password'])

            if isinstance(resp, LoginResponse):
                print("Logged in using a password; saving details to disk")
                self.__write_details_to_disk(resp)
            else:
                print(f"Failed to log in: {resp}")
                sys.exit(1)

    def trust_devices(self, user_id: str, device_list: Optional[str] = None) -> None:
        """Trusts the devices of a user.

        If no device_list is provided, all of the users devices are trusted. If
        one is provided, only the devices with IDs in that list are trusted.

        Arguments:
            user_id {str} -- the user ID whose devices should be trusted.

        Keyword Arguments:
            device_list {Optional[str]} -- The full list of device IDs to trust
                from that user (default: {None})
        """

        print(f"{user_id}'s device store: {self.device_store[user_id]}")

        # The device store contains a dictionary of device IDs and known
        # OlmDevices for all users that share a room with us, including us.

        # We can only run this after a first sync. We have to populate our
        # device store and that requires syncing with the server.
        for device_id, olm_device in self.device_store[user_id].items():
            if device_list and device_id not in device_list:
                # a list of trusted devices was provided, but this ID is not in
                # that list. That's an issue.
                print(
                    f"Not trusting {device_id} as it's not in {user_id}'s pre-approved list."
                )
                continue

            if user_id == self.user_id and device_id == self.device_id:
                # We cannot explicitly trust the device @alice is using
                continue

            self.verify_device(olm_device)
            print(f"Trusting {device_id} from user {user_id}")

    async def cb_autojoin_room(self, room: MatrixRoom, event: InviteEvent):
        """Callback to automatically joins a Matrix room on invite.

        Arguments:
            room {MatrixRoom} -- Provided by nio
            event {InviteEvent} -- Provided by nio
        """
        await self.join(room.room_id)
        # room = self.rooms[room.room_id]
        # print(f"Room {room.name} is encrypted: {room.encrypted}")

    async def cb_to_device_event(self, ver: ToDeviceEvent):
        print(f"\ncb_to_device_event\n{ver=!r}")
    async def cb_key_verify_event(self, ver: KeyVerificationEvent):
        print(f"\ncb_key_verify_event\n{ver=!r}")
    async def cb_key_verify_key(self, ver: KeyVerificationKey):
        print(f"\ncb_key_verify_key\n{ver=!r}")
        mysas = self.get_active_sas('@aibi:chat.yshi.org', 'ATGECGKLBN')
        # mysas.accept_sas()
        print(f"cb_key_verify_key {mysas.__dict__=!r}")
        # mysas.receive_key_event(ver)
        print(f"cb_key_verify_key {mysas.__dict__=!r}")
        print(f"{mysas.get_emoji()=!r}")
        buf = input().strip()
        print(f"{buf=!r}")
        if buf == 'y':
            print(f"{mysas.accept_sas()=!r}")
            # mysas.accept_sas()
            # acc = mysas.accept_verification()
            # print(f"{acc=!r}")
            # resp = await self.to_device(acc)
            # print(f"{resp=!r}")
            acc = mysas.get_mac()
            print(f"{acc=!r}")
            resp = await self.to_device(acc)
            print(f"{resp=!r}")

    async def cb_key_verification_accept(self, ver: KeyVerificationAccept):
        pass
        mysas = self.get_active_sas('@aibi:chat.yshi.org', 'ATGECGKLBN')
        print(f"cb_key_verification_accept {mysas.__dict__=!r}")
        # mysas.receive_accept_event(ver)
        print(f"cb_key_verification_accept {mysas.__dict__=!r}")
        #resp = await self.to_device(mysas.accept_verification())
        #print(f"{resp=!r}")
        #mysas.accept_sas()
        #print(f"{mysas.__dict__=!r}")
        # print(f"{mysas.get_emoji()=!r}")
    async def cb_key_verify_start(self, ver: KeyVerificationStart):
        print(f"\ncb_key_verify_start\n{ver=!r}")
    async def cb_redaction_event(self, room: MatrixRoom, ver: RedactionEvent):
        print(f"\nRedactionEvent\n{room=!r}, {ver=!r}\n\n{ver.__dict__}\n\n")
    async def cb_room_message(self, room: MatrixRoom, event: RoomMessageText):
        print(f"\ncb_room_message({room=!r}, {event=!r})\n")

    async def room_send_unver(self, room_id: str, message_type: str, content, **kwargs):
        kwargs['ignore_unverified_devices'] = True
        parent_event = kwargs.pop('parent_event', None)
        if parent_event:
            event_thread_id = get_event_thread_id(parent_event) or parent_event.event_id
            if 'm.relates_to' not in content:
                content['m.relates_to'] = {
                    'rel_type': 'm.thread',
                    'event_id': event_thread_id,
                }
        return await self.room_send(room_id, message_type, content, **kwargs)

    async def cb_print_messages(self, room: MatrixRoom, event: RoomMessageText):
        """Callback to print all received messages to stdout.

        Arguments:
            room {MatrixRoom} -- Provided by nio
            event {RoomMessageText} -- Provided by nio
        """
        if event.decrypted:
            encrypted_symbol = "🛡 "
        else:
            encrypted_symbol = "⚠️ "
        print(
            f"{room.display_name} |{encrypted_symbol}| {room.user_name(event.sender)}: {event.body}"
        )
        if not isinstance(event, RoomMessageText):
            pass

        event_thread_id = get_event_thread_id(event)
        cmd_lexed = None
        try:
            cmd_lexed = shlex.split(event.body)
        except ValueError:
            pass
        if event_thread_id is not None:
            handler_info = {}
            try:
                with open(f'{event_thread_id}.bot-meta', 'r') as fh:
                    handler_info = json.load(fh)
            except FileNotFoundError:
                pass
            handler = commands.get(handler_info.get('command', ''), None)
            if handler and handler.get('thread_continuation', None):
                thread_continuation = handler['thread_continuation']
                if thread_continuation:
                    return await thread_continuation(self, room, event, event_thread_id)
        if cmd_lexed and cmd_lexed[0] == '!greed-stats':
            cmd_lexed = ['!greed', 'stats'] + cmd_lexed[1:]
        if cmd_lexed and cmd_lexed[0] == '!greederboard':
            cmd_lexed = ['!greed', 'leaderboard'] + cmd_lexed[1:]

        if cmd_lexed and cmd_lexed[0] == '!deer':
            return await self.room_send_unver(room.room_id, "m.reaction", {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": event.event_id,
                    "key": "🦌",
                }
            })
        if cmd_lexed and cmd_lexed[0] == '!pick':
            og_body = event.body
            body_rest = og_body.removeprefix('!pick ')
            if og_body is not body_rest:
                options = body_rest.split(', ')
                (a, ) = random.sample(options, 1)
                return await self.room_send_unver(room.room_id, "m.room.message", {
                    "msgtype": "m.message",
                    "body": a,
                })
            else:
                return await self.room_send_unver(room.room_id, "m.reaction", {
                    "m.relates_to": {
                        "rel_type": "m.annotation",
                        "event_id": event.event_id,
                        "key": "❌",
                    }
                })
        if cmd_lexed and cmd_lexed[0] == '!whoami':
            return await self.room_send_unver(room.room_id, "m.room.message", {
                "msgtype": "m.message",
                "body": event.sender,
            }, parent_event=event)
        if cmd_lexed and cmd_lexed[0] == '!whereami':
            return await self.room_send_unver(room.room_id, "m.room.message", {
                "msgtype": "m.message",
                "body": room.room_id,
            }, parent_event=event)

        if cmd_lexed:
            command_handle = commands.get(cmd_lexed[0], None)
            if command_handle:
                requires_unthreaded = command_handle.get('requires_unthreaded', None)
                if requires_unthreaded is not None:
                    if requires_unthreaded == event_thread_id is not None:
                        command_handle = None
            if command_handle:
                try:
                    args = command_handle['parser'].parse_args(cmd_lexed[1:])
                except MatrixArgumentParserArgumentError as e:
                    return await self.room_send_unver(room.room_id, "m.room.message", {
                        "msgtype": "m.message",
                        "body": str(e),
                    }, parent_event=event)
                handler = command_handle['handler']
                return await handler(self, room, event, args)


    async def cb_audio_file(self, room: MatrixRoom, event: RoomEncryptedAudio):
        resp = await self.download(event.url)
        if not isinstance(resp, DownloadResponse):
            print(f"early return: {type(resp)=!r}/{type(resp).__mro__=!r}/{resp=!r}")
            return
        # if event.mimetype != 'audio/ogg':
        #     return
        transcribed = None
        async for e in transcribe_audio_file(room, event, resp):
            if e is EVENT_STARTED:
                await self.room_send_unver(room.room_id, "m.reaction", {
                    "m.relates_to": {
                        "rel_type": "m.annotation",
                        "event_id": event.event_id,
                        "key": "🤔",
                    }
                })
            if isinstance(e, EventCompleted):
                transcribed = e.value
                await self.room_send_unver(room.room_id, "m.reaction", {
                    "m.relates_to": {
                        "rel_type": "m.annotation",
                        "event_id": event.event_id,
                        "key": "✅",
                    }
                })
                break
        else:
            return
        return await self.room_send_unver(room.room_id, "m.room.message", {
            "msgtype": "m.text",
            "body": transcribed,
            'm.relates_to': {
                'rel_type': 'm.thread',
                'event_id': get_event_thread_id(event) or event.event_id,
            }
        })


    async def send_hello_world(self):
        # Now we send an encrypted message that @bob can read, although it will
        # appear to be "unverified" when they see it, because @bob has not verified
        # the device @alice is sending from.
        # We'll leave that as an exercise for the reader.
        try:
            
            await self.room_send_unver(
                room_id=ROOM_ID,
                message_type="m.room.message",
                content={
                    "msgtype": "m.message",
                    "body": "Hello, this message is encrypted",
                },
            )
        except exceptions.OlmUnverifiedDeviceError as err:
            print("These are all known devices:")
            device_store: crypto.DeviceStore = self.device_store
            [
                print(
                    f"\t{device.user_id}\t {device.device_id}\t {device.trust_state}\t  {device.display_name}"
                )
                for device in device_store
            ]
            # sys.exit(1)

        devices = self.room_devices(ROOM_ID)
        print(f"{devices=!r}")

    def __write_details_to_disk(self, resp: LoginResponse) -> None:
        """Writes login details to disk so that we can restore our session later
        without logging in again and creating a new device ID.

        Arguments:
            resp {LoginResponse} -- the successful client login response.
        """
        with open(self._bot_config['credentials-file'], "w") as f:
            json.dump(
                {
                    "access_token": resp.access_token,
                    "device_id": resp.device_id,
                    "user_id": resp.user_id,
                },
                f,
            )


async def run_client(client: CustomEncryptedClient) -> None:
    """A basic encrypted chat application using nio."""

    # This is our own custom login function that looks for a pre-existing config
    # file and, if it exists, logs in using those details. Otherwise it will log
    # in using a password.
    await client.login()
    if client.should_upload_keys:
        await client.keys_upload()

    # Here we create a coroutine that we can call in asyncio.gather later,
    # along with sync_forever and any other API-related coroutines you'd like
    # to do.
    async def after_first_sync():
        # We'll wait for the first firing of 'synced' before trusting devices.
        # client.synced is an asyncio event that fires any time nio syncs. This
        # code doesn't run in a loop, so it only fires once
        print("Awaiting sync")
        await client.synced.wait()

        # In practice, you want to have a list of previously-known device IDs
        # for each user you want to trust. Here, we require that list as a
        # global variable
        #client.trust_devices('@aibi:chat.yshi.org', ["DINJYVSQGR", "ATGECGKLBN", "DXYABNVOOE"])
        #client.trust_devices("@sell:m-test.yyc1.yshi.org", ["INQWEJRDPS"])
        #client.trust_devices("@sell2:m-test.yyc1.yshi.org", ["BNATKLNMQK"])
        # client.create_key_verification()
        if False:
            print(f"{client.room_contains_unverified(ROOM_ID)=!r}")
            print(f"{client.users_for_key_query=!r}")
            print(f"{client.room_devices(ROOM_ID)=!r}")
            room_device = client.room_devices('!XSskJMyVdwwckSNECF:m-test.yyc1.yshi.org')
            ver = client.create_key_verification(room_device['@aibi:chat.yshi.org']['ATGECGKLBN'])
            print(f"{ver=!r}")
            resp = await client.to_device(ver)
            print(f"{resp=!r}")
            # resp = KeyVerificationStart.from_dict(resp.as_dict())
            print(f"{resp=!r}")

    # We're creating Tasks here so that you could potentially write other
    # Python coroutines to do other work, like checking an API or using another
    # library. All of these Tasks will be run concurrently.
    # For more details, check out https://docs.python.org/3/library/asyncio-task.html

    # ensure_future() is for Python 3.5 and 3.6 compatibility. For 3.7+, use
    # asyncio.create_task()
    after_first_sync_task = asyncio.ensure_future(after_first_sync())

    # We use full_state=True here to pull any room invites that occurred or
    # messages sent in rooms _before_ this program connected to the
    # Matrix server
    sync_forever_task = asyncio.ensure_future(
        client.sync_forever(30000, full_state=True)
    )

    await asyncio.gather(
        # The order here IS significant! You have to register the task to trust
        # devices FIRST since it awaits the first sync
        after_first_sync_task,
        sync_forever_task,
    )


async def main(args):
    # By setting `store_sync_tokens` to true, we'll save sync tokens to our
    # store every time we sync, thereby preventing reading old, previously read
    # events on each new sync.
    # For more info, check out https://matrix-nio.readthedocs.io/en/latest/nio.html#asyncclient
    config = ClientConfig(store_sync_tokens=True)
    with open(args.config) as fh:
        config_doc = json.load(fh)

    client = CustomEncryptedClient(
        config_doc['home-server'],
        config_doc['user'],
        store_path=config_doc['nio-store'],
        config=config,
        ssl=True,
        bot_config=config_doc,
    )

    try:
        await run_client(client)
    except (asyncio.CancelledError, KeyboardInterrupt):
        await client.close()


# Run the main coroutine, which instantiates our custom subclass, trusts all the
# devices, and syncs forever (or until your press Ctrl+C)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

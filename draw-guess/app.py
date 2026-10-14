"""실시간 멀티플레이 그림 퀴즈 게임 - FastAPI + WebSocket"""
import asyncio
import json
from pathlib import Path
import random
import string
import time
from dataclasses import dataclass, field
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

app = FastAPI(title="그림퀴즈")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# ─── 게임 설정 ──────────────────────────────────────────────────
QUIZ_TIME = 60         # 출제 단계: 제시어 입력 시간(초) — 60초 안에 내지 못하면 skip_round
ROUND_TIME = 60        # 드로잉 시간(초)
PREP_TIME = 3          # 라운드 준비 시간(초)
MIN_ROUNDS = 1         # 호스트 선택 가능 최소 라운드 수
MAX_ROUNDS = 3         # 호스트 선택 가능 최대 라운드 수
MIN_PLAYERS = 2        # 최소 참가 인원
MAX_PLAYERS = 8        # 최대 참가 인원
WORD_MIN_LEN = 1       # 제시어 최소 글자 수 (비어 있는 경우만 불가)
WORD_MAX_LEN = 20      # 제시어 최대 글자 수
CORRECT_SCORE = 100    # 정답자당 고정 점수

# ─── WebSocket send 직렬화 ──────────────────────────────────────
# 같은 소켓에 대한 send_text가 동시에 나가면 "already connected" 오류가
# 발생하므로, 소켓별로 락을 두어 send를 직렬화한다.



def make_room_code() -> str:
    return "".join(random.choices(string.digits, k=6))


@dataclass
class Player:
    pid: str
    nickname: str
    ws: Optional[WebSocket] = None
    score: int = 0
    round_scores: list = field(default_factory=list)
    connected: bool = True
    outq: asyncio.Queue = field(default_factory=asyncio.Queue)


@dataclass
class Room:
    code: str
    host_pid: str
    players: dict = field(default_factory=dict)  # pid -> Player
    state: str = "lobby"  # lobby | quiz | prep | drawing | round_end | game_over
    rounds: int = MIN_ROUNDS          # 호스트가 선택한 사이클 수 (1~3)
    round_time: int = ROUND_TIME      # 호스트가 선택한 드로잉 제한시간(초), 0 = 제한 없음
    total_rounds: int = 0             # 총 판수 = 연결 플레이어 수 × rounds
    current_round: int = 0
    current_word: str = ""      # 출제자가 직접 입력한 제시어
    quizzer_pid: str = ""       # 이번 라운드 출제자 (제시어 입력할 사람)
    drawer_pid: str = ""        # 그림 그릴 사람 (출제자와 반드시 다름)
    correct_answered: list = field(default_factory=list)  # [(pid, score)]
    timer_task: Optional[asyncio.Task] = None
    created_at: float = field(default_factory=time.time)
    tiebreak_used: bool = False   # 동점 시 결승전 1판 추가 여부 (한 게임에 한 번만)
    tiebreak_active: bool = False # 현재 라운드가 결승전인지 — 다시 동점이면 그대로 종료
    tiebreak_players: list = field(default_factory=list)  # 결승전 참가자 pid (동점자들만)
    skip_prep_next: bool = False  # 직전 라운드 timeout 패스 → 다음 라운드 prep 카운트다운 생략


rooms: dict[str, Room] = {}
ws_to_room: dict[WebSocket, str] = {}  # ws -> room_code


def get_player(ws: WebSocket) -> tuple[Room, Player]:
    code = ws_to_room.get(ws)
    if not code or code not in rooms:
        raise ValueError("room not found")
    room = rooms[code]
    # find player by ws
    for p in room.players.values():
        if p.ws is ws:
            return room, p
    raise ValueError("player not found")


def _enqueue(room: Room, pid: str, payload: str):
    """플레이어의 outq에 메시지를 넣는다. 실제 send는 해당 플레이어의 수신 루프에서만 한다."""
    p = room.players.get(pid)
    if p and p.connected:
        p.outq.put_nowait(payload)


async def _drain_outq(player: Player) -> None:
    """큐가 비어질 때까지 메시지를 소켓으로 보낸다. 메인 루프만 호출한다.

    send_text에 5초 타임아웃을 붙여 느린 클라이언트 한 대가 이벤트 루프를
    무한정 점유하는 것을 막는다. 타임아웃이 발생하면 해당 플레이어의 연결을
    끊고 나머지 큐는 폐기한다 — 다른 플레이어의 지연은 방지된다.
    """
    ws = player.ws
    while not player.outq.empty():
        payload = player.outq.get_nowait()
        try:
            await asyncio.wait_for(ws.send_text(payload), timeout=5.0)
        except (asyncio.TimeoutError, Exception):
            # 전송 실패 또는 타임아웃 — 이 플레이어 연결 종료
            player.connected = False
            try:
                await ws.close(code=4008)
            except Exception:
                pass
            # 남은 큐 메시지 폐기
            while not player.outq.empty():
                player.outq.get_nowait()
            break


async def broadcast(room: Room, msg: dict):
    payload = json.dumps(msg, ensure_ascii=False)
    for p in list(room.players.values()):
        if p.connected:
            p.outq.put_nowait(payload)


async def send_to(room: Room, pid: str, msg: dict):
    _enqueue(room, pid, json.dumps(msg, ensure_ascii=False))


def room_state_msg(room: Room) -> dict:
    return {
        "type": "state",
        "code": room.code,
        "state": room.state,
        "currentRound": room.current_round,
        "totalRounds": room.total_rounds or (len(room.players) * room.rounds),
        "players": [
            {"pid": p.pid, "nickname": p.nickname, "score": p.score,
             "roundScores": p.round_scores, "connected": p.connected}
            for p in room.players.values()
        ],
        "drawerPid": room.drawer_pid,
        "quizzerPid": room.quizzer_pid,
        "correctAnswered": [
            {"pid": pid, "score": s} for pid, s in room.correct_answered
        ],
        "skipPrepNext": getattr(room, "skip_prep_next", False),
    }


_quiz_events: dict[str, tuple[int, asyncio.Event]] = {}


def _get_quiz_event(code: str, round_num: int) -> asyncio.Event:
    """라운드별 고유 Event — 이전 라운드의 stale set/clear가 새 라운드에 영향을 주지 못한다."""
    cur = _quiz_events.get(code)
    if cur is None or cur[0] != round_num:
        ev = asyncio.Event()
        _quiz_events[code] = (round_num, ev)
        return ev
    return cur[1]


async def _wait_quiz_ev(ev: asyncio.Event, timeout: float) -> bool:
    """wait_for 대신 수동 타이머 — 타임아웃 시 이벤트 상태를 보존한다.
    wait_for는 cancel로 인해 set된 이벤트 상태까지 지워버려 race condition을 만든다."""
    try:
        await asyncio.wait_for(asyncio.shield(ev.wait()), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


async def _handle_correct_answer(room: Room, pid: str):
    """정답을 맞힌 플레이어에게 점수를 주고, 아직 아무도 맞히지 않았다면 라운드를 끝낸다.

    - 첫 번째 정답자만 라운드를 종료한다. 이후의 정답은 채팅으로만 표시되고
      다음 라운드까지 기다린다. (이전처럼 매 정답마다 end_round가 호출되면
      라운드가 끝나고 game_over/다음 라운드로 넘어가는 순간 클라이언트가
      "방에서 튕겨 나가는 것처럼" 보이는 문제가 발생했다.)
    - 이미 정답을 맞힌 플레이어는 중복 체크를 하지 않는다.
    """
    if room.state != "drawing":
        return
    # 이미 맞힌 사람은 다시 처리하지 않음
    if any(c[0] == pid for c in room.correct_answered):
        return
    rank = len(room.correct_answered) + 1
    score = CORRECT_SCORE
    player = room.players.get(pid)
    if not player:
        return
    player.score += score
    player.round_scores.append(score)
    room.correct_answered.append((pid, score))
    await broadcast(room, {
        "type": "correct", "pid": pid,
        "nickname": player.nickname, "score": score
    })
    # 첫 정답자만 라운드를 끝낸다 — 그 뒤의 정답자는 채팅으로만 표시
    if rank == 1:
        await end_round(room)


async def run_round(room: Room):
    """한 라운드를 진행하는 타이머 루프: (드로어)출제 → 준비 → 드로잉."""
    # ── 1단계: 출제 — 이번 라운드 그리는 사람(drawer)이 직접 제시어 입력 ──
    room.state = "quiz"
    room.current_word = ""
    ev = _get_quiz_event(room.code, room.current_round)
    await broadcast(room, {**room_state_msg(room), "timeLeft": QUIZ_TIME})

    # QUIZ_TIME 동안 drawer 의 submit_word 를 기다린다.
    # prep/drawing과 달리 quiz 단계에도 매 초 tick을 보내 화면의 남은 시간이
    # 실제로 줄어들게 한다. 출제자가 문제를 내면(ev set) 즉시 중단하고
    # prep으로 넘어간다. 시간 초과 시에는 아래 skip_round로 처리한다.
    for i in range(QUIZ_TIME - 1, 0, -1):
        await asyncio.sleep(1)
        if ev.is_set():
            break  # 출제 완료 — 바로 준비 단계로
        if room.state != "quiz":
            return  # 게임 종료 등으로 이미 넘어간 경우
        await broadcast(room, {"type": "tick", "timeLeft": i})
    got = ev.is_set()
    if not got:
        if room.state != "quiz":
            return  # 이미 다른 경로(게임 종료 등)로 넘어간 경우
        # 60초 안에 문제를 내지 못하면 재기회 없이 즉시 다음 라운드로 진행한다.
        await skip_round(room, reason="timeout")
        return

    # ── 2단계: 준비 ──
    # 직전 라운드가 timeout 패스였다면 prep 카운트다운 없이 바로 drawing으로 간다.
    if room.skip_prep_next:
        room.skip_prep_next = False
    else:
        room.state = "prep"
        await broadcast(room, {**room_state_msg(room), "word": "", "timeLeft": PREP_TIME})
        # 카운트다운: 각 숫자를 정확히 1초씩 노출한다.
        # state 메시지에 timeLeft=3 포함 → t=0s "3" 표시
        # 이후 매 초마다 2, 1을 tick으로 전송 → t=1s "2", t=2s "1", t=3s drawing
        for i in range(PREP_TIME - 1, 0, -1):
            await asyncio.sleep(1)
            await broadcast(room, {"type": "tick", "timeLeft": i})
        # 마지막 1초 대기 — "1"이 1초간 화면에 머물렀다가 drawing으로 전환
        await asyncio.sleep(1)

    # ── 3단계: 드로잉 — 제시어는 드로어에게만 전송 ──
    room.state = "drawing"
    room.correct_answered = []
    # 새 라운드 진입 시 화이트보드를 초기화한다. draw(clear) 브로드캐스트가
    # state 메시지에 이어지므로 모든 클라이언트는 prep/round_end 화면에서
    # 캔버스를 지운 뒤 drawing UI를 표시하게 된다.
    await broadcast(room, {"type": "draw", "clear": True})
    await send_to(room, room.drawer_pid, {
        "type": "word_reveal", "word": room.current_word
    })
    # 호스트가 방 생성 시 선택한 제한시간 사용 (0 = 제한 없음).
    # 제한이 있으면 매 초 tick을 보내며 0초까지 기다렸다가 end_round.
    # 제한이 없으면(0) tick 없이 정답 또는 상태 변경을 무기한 대기한다.
    rt = room.round_time
    await broadcast(room, {**room_state_msg(room), "timeLeft": rt})

    if rt > 0:
        for i in range(rt, 0, -1):
            await asyncio.sleep(1)
            await broadcast(room, {"type": "tick", "timeLeft": i})
            if room.state != "drawing":
                break
    else:
        # 제한 없음 — 상태가 drawing에서 벗어날 때까지 1초 간격으로 폴링
        while room.state == "drawing":
            await asyncio.sleep(1)

    # _handle_correct_answer may have already called end_round (state != "drawing").
    # Only call end_round here if the round hasn't been ended yet.
    if room.state == "drawing":
        await end_round(room)


async def skip_round(room: Room, reason: str):
    """제시어가 없는 라운드를 건너뛴다 (점수 부여 안 함).

    timeout(출제자가 60초 안에 문제를 내지 못한 경우)이면 클라이언트가
    팝업으로 안내할 시간을 주기 위해 10초를 기다린 뒤 다음 라운드로 넘어간다.
    마지막 라운드라면 10초 대기 없이 바로 game_over로 전환한다.
    """
    if room.state == "round_end":
        return  # 이미 다른 경로(end_round)로 라운드가 종료된 경우
    room.state = "round_end"
    room.correct_answered = []
    is_last = room.current_round >= room.total_rounds
    # 마지막 라운드 timeout이면 skippedReason="timeout_last"으로 구분하여
    # 클라이언트가 timeout 팝업을 띄우지 않고 즉시 game_over 화면으로 간다.
    skip_reason = "timeout_last" if (reason == "timeout" and is_last) else reason
    msg = {**room_state_msg(room), "word": "", "timeLeft": 0, "skippedReason": skip_reason}
    await broadcast(room, msg)
    if reason == "timeout" and not is_last:
        # 클라이언트가 10초 카운트다운 팝업을 표시하고 자동으로 닫히게 한다.
        room.skip_prep_next = True  # 다음 라운드 prep 카운트다운 생략
        # 각 클라이언트의 setInterval 오차로 팝업 종료 시점이 기기마다 달라지는 문제를 막기 위해
        # 서버가 매 초 tick을 보내고, 마지막 tick(timeLeft=0) 수신 시점에 클라이언트가 팝업을 닫는다.
        for i in range(9, -1, -1):
            await broadcast(room, {"type": "tick", "timeLeft": i})
            await asyncio.sleep(1)
    if is_last:
        await _finish_or_tiebreak(room)
    else:
        start_next_round(room)


async def end_round(room: Room):
    if room.state == "round_end":
        return  # 이미 종료 처리됨 — _handle_correct_answer와 drawing 루프의 이중 호출 방지
    room.state = "round_end"
    msg = {**room_state_msg(room), "word": room.current_word, "timeLeft": 0}
    await broadcast(room, msg)

    # round_end 화면은 클라이언트가 timeLeft:0 수신 후 자체 타이머로 표시
    if room.current_round >= room.total_rounds:
        await _finish_or_tiebreak(room)
    else:
        # 클라이언트가 정답 공개 팝업(10초 카운트다운)을 표시하는 동안
        # 다음 라운드 출제 타이머가 시작되지 않도록 서버도 10초 대기한다.
        for i in range(9, -1, -1):
            await asyncio.sleep(1)
        start_next_round(room)


async def _finish_or_tiebreak(room: Room):
    """모든 라운드가 끝난 시점의 처리.

    - 3인 이상 게임에서 최고득점자끼리 2명 이상 동점이면, 그 동점자들만
      참여하는 결승전 1라운드를 시작한다. 출제자는 관전자 중 랜덤으로 결정된다.
    - 2인 게임은 동점이어도 결승전 없이 그대로 종료한다.
    - 결승전에서 다시 동점이면 그대로 동점 처리로 게임을 완전히 종료한다.
      (결승전은 항상 최대 1라운드)
    - 동점이 아니면 즉시 game_over로 전환한다.
    """
    connected = [p for p in room.players.values() if p.connected]
    if room.tiebreak_active:
        # 결승전 1라운드 종료 — 다시 동점이면 그대로 동점 처리로 종료
        room.tiebreak_active = False
        room.tiebreak_players = []
        room.state = "game_over"
        await broadcast(room, {**room_state_msg(room), "word": room.current_word})
        return
    if not room.tiebreak_used and len(connected) >= 3:
        top_score = max(p.score for p in connected)
        tied = [p.pid for p in connected if p.score == top_score]
        if len(tied) >= 2:
            # 동점자 2명 이상 — 그들만 참여하는 결승전 1라운드
            room.tiebreak_used = True
            room.tiebreak_active = True
            room.tiebreak_players = tied
            room.total_rounds += 1
            msg = {**room_state_msg(room), "tiebreak": True}
            await broadcast(room, msg)
            # 클라이언트가 정답 공개 팝업(10초 카운트다운)을 표시하는 동안
            # 다음 라운드 출제 타이머가 시작되지 않도록 서버도 10초 대기한다.
            for i in range(9, -1, -1):
                await asyncio.sleep(1)
            start_next_round(room)
            return
    room.state = "game_over"
    await broadcast(room, {**room_state_msg(room), "word": room.current_word})


def start_next_round(room: Room):
    room.current_round += 1
    pids = [p.pid for p in room.players.values() if p.connected]
    if len(pids) < MIN_PLAYERS:
        # 사람이 부족하면 게임 종료
        room.state = "game_over"
        asyncio.create_task(broadcast(room, room_state_msg(room)))
        return
    if room.tiebreak_active:
        # 결승전 라운드 — 출제자는 관전자(동점자 제외) 중 랜덤으로 결정
        observers = [pid for pid in pids if pid not in room.tiebreak_players]
        room.drawer_pid = random.choice(observers) if observers else random.choice(pids)
    else:
        # 일반 라운드 — 모든 플레이어가 한 사이클에 한 번씩 출제 (player_list[i % n])
        player_list = list(room.players.keys())
        n = len(player_list)
        room.drawer_pid = player_list[(room.current_round - 1) % n]
    room.quizzer_pid = room.drawer_pid
    room.current_word = ""          # 시스템은 정답은 만들지 않는다 — 출제자가 직접 입력
    room.correct_answered = []
    _get_quiz_event(room.code, room.current_round).clear()  # 새 라운드에서 quiz 이벤트를 초기화
    room.timer_task = asyncio.create_task(run_round(room))


@app.websocket("/ws/{room_code}")
async def websocket_endpoint(ws: WebSocket, room_code: str):
    await ws.accept()
    code = room_code.upper()

    try:
        # 첫 메시지: join
        raw = await ws.receive_text()
        data = json.loads(raw)
        action = data.get("action")

        if action == "create":
            nickname = data.get("nickname", "").strip()[:12]
            if not nickname:
                await ws.close(code=4001)
                return
            # 새 방 생성
            # 호스트가 1~3라운드 중 선택 (기본값은 MIN_ROUNDS)
            rounds = int(data.get("rounds", MIN_ROUNDS))
            if not (MIN_ROUNDS <= rounds <= MAX_ROUNDS):
                rounds = MIN_ROUNDS
            # 호스트가 선택한 드로잉 제한시간 — 클라이언트 드롭다운 값(15/30/60/90/120/180/300/0)만 허용.
            # 0은 "제한 없음". 알 수 없는 값은 기본값 ROUND_TIME으로.
            guess_time = int(data.get("guess_time", ROUND_TIME))
            if guess_time not in (15, 30, 60, 90, 120, 180, 300, 0):
                guess_time = ROUND_TIME
            while code in rooms:
                code = make_room_code()
            room = Room(code=code, host_pid="", rounds=rounds, round_time=guess_time)
            pid = f"{code}_{len(room.players)+1}"
            player = Player(pid=pid, nickname=nickname, ws=ws)
            room.players[pid] = player
            room.host_pid = pid
            rooms[code] = room
            ws_to_room[ws] = code

        elif action == "join":
            nickname = data.get("nickname", "").strip()[:12]
            if not nickname:
                await ws.close(code=4001)
                return
            room = rooms.get(code)
            if not room:
                await ws.send_text(json.dumps({"type": "error", "message": "방을 찾을 수 없습니다"}))
                await ws.close(code=4004)
                return
            if len(room.players) >= MAX_PLAYERS:
                await ws.send_text(json.dumps({"type": "error", "message": "방이 가득 찼습니다"}))
                await ws.close(code=4004)
                return
            pid = f"{code}_{len(room.players)+1}"
            player = Player(pid=pid, nickname=nickname, ws=ws)
            room.players[pid] = player
            ws_to_room[ws] = code

        else:
            await ws.close(code=4000)
            return

        # 입장 확인
        await send_to(room, pid, {"type": "joined", "pid": pid, "code": room.code,
                                  "nickname": nickname, "isHost": pid == room.host_pid})
        await broadcast(room, room_state_msg(room))

        # 메인 루프: receive_text에 0.5초 타이머를 붙여 주기적으로 자기 outq를 비운다.
        # send는 항상 이 태스크에서 하므로 소켓 경쟁 상태가 없다.
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=0.1)
            except asyncio.TimeoutError:
                pass
            except WebSocketDisconnect:
                raise
            else:
                data = json.loads(raw)
                act = data.get("action")

                if act == "start_game":
                    # 호스트만 시작 가능
                    if pid != room.host_pid:
                        continue
                    if len([p for p in room.players.values() if p.connected]) < MIN_PLAYERS:
                        await send_to(room, pid, {"type": "error", "message": f"최소 {MIN_PLAYERS}명이 필요합니다"})
                        continue
                    # 초기화 — 총 판수 = 연결 플레이어 수 × rounds
                    connected = [p for p in room.players.values() if p.connected]
                    room.total_rounds = len(connected) * room.rounds
                    for p in room.players.values():
                        p.score = 0
                        p.round_scores = []
                    room.current_round = 0
                    room.tiebreak_used = False
                    start_next_round(room)
                elif act == "submit_word":
                    # 드로어가 직접 제시어 입력 — 그 사람 이름으로 그림을 그린다
                    # drawer 검증만 하고 state는 체크하지 않는다:
                    # start_next_round가 create_task(run_round)만 하고 return하므로,
                    # B의 submit_word가 run_round보다 먼저 arrive하면 state가 아직 "round_end"다.
                    # word를 저장하고 ev.set()하면 run_round가 wait()에 들어가기 전이면
                    # set된 이벤트가 이미 true이므로 wait()이 즉시 return한다.
                    if pid != room.drawer_pid:
                        continue
                    word = data.get("word", "").strip()[:WORD_MAX_LEN]
                    if len(word) < WORD_MIN_LEN:
                        await send_to(room, pid, {"type": "error", "message": f"제시어는 {WORD_MIN_LEN}자 이상 입력하세요"})
                        continue
                    room.current_word = word
                    _get_quiz_event(room.code, room.current_round).set()   # run_round 대기 해제 → 준비 단계로

                elif act == "chat":
                    text = data.get("text", "").strip()[:100]
                    if not text:
                        continue
                    # 채팅 브로드캐스트
                    await broadcast(room, {
                        "type": "chat", "pid": pid,
                        "nickname": room.players[pid].nickname, "text": text
                    })
                    # 정답 체크 (드로잉 중이고, 드로어가 아닌 경우)
                    # 단, 이미 정답을 맞힌 플레이어는 다시 체크하지 않는다.
                    # 채팅 입력이 연속으로 들어오면(예: 정답 제출 후 Enter를 한 번 더 누르면
                    # 같은 텍스트가 재전송됨) 매번 end_round가 호출되어 라운드가 끝나는 순간
                    # 클라이언트가 방에서 튕겨 나오는 것처럼 보이는 문제를 막는다.
                    if (room.state == "drawing" and pid != room.drawer_pid
                            and pid not in {c[0] for c in room.correct_answered}):
                        answer = text.lower().replace(" ", "")
                        word = room.current_word.lower().replace(" ", "")
                        if answer == word:
                            # end_round 내부의 sleep(4)이 채팅 처리 코루틴을 막으면
                            # 정답을 맞힌 본인(outq drain이 이 루프에만 있으므로)의
                            # correct/round_end 메시지가 4초 뒤까지 전송되지 않는다.
                            # 별도 태스크로 분리해 채팅 루프는 즉시 돌아간다.
                            asyncio.create_task(_handle_correct_answer(room, pid))

                elif act == "draw":
                    points = data.get("points", [])
                    color = data.get("color", "#000000")
                    size = data.get("size", 3)
                    clear = bool(data.get("clear", False))
                    if room.state == "drawing" and pid == room.drawer_pid:
                        # draw 스트로크를 드로어 본인에게도 브로드캐스트한다.
                        # 클라이언트는 strokeMove에서 자기 캔버스에 직접 그리는 동시에 서버에도
                        # 보내는데, 이 두 경로의 순서가 뒤섞이면(예: state 재전송이 중간에 끼면
                        # resetCanvas가 원격 스트로크보다 늦게 실행됨) 상대 화면에는 선이 남아있고
                        # 드로어 화면에서는 사라지는 불일치가 생긴다. 서버가 단일 소스로 모든
                        # 참가자(드로어 포함)의 캔버스를 갱신하면 양쪽이 항상 같은 상태를 유지한다.
                        await broadcast(room, {
                            "type": "draw", "pid": pid,
                            "points": points, "color": color, "size": size,
                            "clear": clear
                        })

                elif act == "leave":
                    break

                else:
                    continue

            # 수신 사이클마다 큐에 쌓인 메시지를 소켓으로 보낸다.
            await _drain_outq(player)

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        # 접속 해제 처리
        if ws in ws_to_room:
            code = ws_to_room.pop(ws)
            room = rooms.get(code)
            if room:
                for p in room.players.values():
                    if p.ws is ws:
                        p.connected = False
                        p.ws = None
                # 방이 비면 삭제
                if not any(p.connected for p in room.players.values()):
                    del rooms[code]
                else:
                    await broadcast(room, room_state_msg(room))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

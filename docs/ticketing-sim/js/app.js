// ============================================================
// 티켓팅 시뮬레이터 - 메인 로직
// ============================================================

const state = {
  mode: "concert",
  running: false,
  seats: [],        // { id, row, col, type, price, taken }
  bots: [],
  startTime: null,
};

let _actualTotalSeats = null; // 각 구역 actualSeatCount 합계 — renderConcertSeats에서 계산

// ---------- 예약 진행 단계 (보안문자 → 날짜/시간 → 구역 → 좌석 → 결제) ----------
const _flow = {
  step: "home",            // home | queue | captcha | datetime | zone | seats | payment (datetime: 목표값 검증)
  captchaCode: "",         // 현재 화면에 표시 중인 보안 문자
  selectedDay: null,       // 선택한 공연 날짜 ({label})
  selectedTime: null,      // 선택한 공연 시간 ({label})
  selectedZone: null,      // 선택한 구역 id
};

// 모든 .mode-screen 에서 active를 빼고 특정 화면만 active로 만든다.
function showFlowScreen(id) {
  document.querySelectorAll(".mode-screen").forEach(s => s.classList.remove("active"));
  const el = document.getElementById(id);
  if (el) el.classList.add("active");
}

// 시계/대기열 오버레이 숨기기 (좌석 단계 진입 때 쓴 로직을 그대로 재사용)
function hideClockAndQueueOverlays() {
  stopQueue();
  const clockOverlay = document.getElementById("clock-overlay");
  if (clockOverlay) { clockOverlay.classList.add("hidden"); clockOverlay.classList.remove("open"); }
  const queueOverlay = document.getElementById("queue-overlay");
  if (queueOverlay) queueOverlay.classList.add("hidden");
}

// ---------- 랜덤 날짜/시간 제시 (시작 버튼 클릭 시점 한 번만 확정) ----------
const OFFERED_TIMES = ["15:00", "17:00", "19:00", "20:30"];
function rollOfferedDateTime() {
  // 오늘 기준 5일 중 랜덤 1일 + 상수 시간 목록 중 랜덤 1개
  const base = new Date();
  const offsetDays = Math.floor(Math.random() * 5); // 0~4
  const d = new Date(base.getFullYear(), base.getMonth(), base.getDate() + offsetDays);
  const wdNames = ["일", "월", "화", "수", "목", "금", "토"];
  const dayLabel = `${d.getMonth() + 1}월 ${d.getDate()}일 (${wdNames[d.getDay()]})`;
  const timeLabel = OFFERED_TIMES[Math.floor(Math.random() * OFFERED_TIMES.length)];
  _flow.offeredDay = dayLabel;
  _flow.offeredTime = timeLabel;
}
function getConcertNameInputValue() {
  const inp = document.getElementById("concert-name-input");
  return inp ? inp.value.trim() : "";
}
function applyConcertNameToConfig() {
  const name = getConcertNameInputValue();
  if (!name) return;
  CONFIG.concert.name = name;
  const el = document.querySelector('[data-key="concert.name"]');
  if (el) el.textContent = name;
}

// ---------- 시작하기 버튼 : 공연명 입력 시에만 활성화, 클릭하면 시계 오버레이 시작 ----------
(function bindConcertStart() {
  const btn = document.getElementById("btn-concert-start");
  if (!btn) return;
  // 공연명이 비어 있으면 시작하기 버튼을 비활성화한다.
  const nameInput = document.getElementById("concert-name-input");
  if (nameInput) {
    const syncBtnState = () => { btn.disabled = !nameInput.value.trim(); };
    nameInput.addEventListener("input", syncBtnState);
    syncBtnState();
  }
  btn.addEventListener("click", () => {
    // 시작과 동시에 공연명을 config에 반영하고, 랜덤 날짜/시간을 확정한다.
    applyConcertNameToConfig();
    rollOfferedDateTime();
    // 정상 흐름: 오픈까지 카운트다운 시계 오버레이 열기 + 30초 타이머 시작
    const overlay = document.getElementById("clock-overlay");
    if (overlay) {
      overlay.classList.remove("hidden");
      overlay.classList.remove("open");
    }
    // 시계 화면에도 공연명 노출 ("티켓팅 오픈까지" 문구 앞에)
    const clockNameEl = document.getElementById("clock-concert-name");
    if (clockNameEl) clockNameEl.textContent = getConcertNameInputValue();
    // 30초 카운트다운을 실제로 돌린다. 없으면 숫자가 11:59:30에서 멈춘다.
    startTicketClock();
  });
  // "처음으로" 버튼으로 홈으로 돌아갈 때 다음 런에도 의미가 있도록 제안값 초기화
  const backBtn = document.getElementById("btn-back-home");
  if (backBtn) backBtn.addEventListener("click", () => {
    _flow.offeredDay = null;
    _flow.offeredTime = null;
  });
})();
// ---------- 단계 1: 보안 문자 입력 ----------
function newCaptchaCode() {
  const chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"; // 헷갈리는 I/O/0/1 제외
  let s = "";
  for (let i = 0; i < 6; i++) s += chars[Math.floor(Math.random() * chars.length)];
  return s;
}

function enterCaptchaStep() {
  hideClockAndQueueOverlays(); // 대기열/시계 오버레이 닫기 — 0명 도달 시 자동 전환에 필수
  _flow.step = "captcha";
  _flow.captchaCode = newCaptchaCode();
  const box = document.getElementById("captcha-text");
  const input = document.getElementById("captcha-input");
  const err = document.getElementById("captcha-error");
  if (box) box.textContent = _flow.captchaCode;
  if (input) { input.value = ""; input.focus(); }
  if (err) err.classList.add("hidden");
  showFlowScreen("screen-captcha");
}

function checkCaptcha() {
  const input = document.getElementById("captcha-input");
  const err = document.getElementById("captcha-error");
  // 화면의 text-transform:uppercase는 표시만 바꾸고 .value는 그대로라,
  // 소문자로 입력하면 완전 일치 비교에서 실패했다. 대문자로 정규화해 비교한다.
  const val = input ? input.value.trim().toUpperCase() : "";
  if (val === _flow.captchaCode.toUpperCase()) {
    enterDateTimeStep();
  } else {
    if (err) err.classList.remove("hidden");
    if (input) { input.value = ""; input.focus(); }
  }
}

// ---------- 단계 2: 날짜 / 시간 선택 ----------
function enterDateTimeStep() {
  _flow.step = "datetime";
  _flow.selectedDay = null;
  _flow.selectedTime = null;
  hideDtError();
  hideNotTargetPopup();
  updateDtTargetDisplay();
  renderDateList();
  showFlowScreen("screen-datetime");
}

// 날짜/시간 선택 단계: 모든 버튼은 비어 있고(값이 안 보임), 화면 오른쪽 위에
// 제시된 목표 날짜/시간만 표시된다. 사용자는 목표를 보고 올바른 버튼을
// 골라야 하며, 다른 값을 누르면 '목표 회차가 아닙니다!' 팝업이 뜨고
// 다음 단계로 진행되지 않는다.
function showNotTargetPopup() {
  const popup = document.getElementById("not-target-popup");
  if (popup) popup.classList.remove("hidden");
}
function hideNotTargetPopup() {
  const popup = document.getElementById("not-target-popup");
  if (popup) popup.classList.add("hidden");
}
// 다음 단계로 넘어갈 수 있는 조건: 선택값이 모두 목표값과 일치하는 경우만 true
function dtSelectionComplete() {
  return !!(
    _flow.selectedDay && _flow.selectedTime &&
    _flow.offeredDay && _flow.offeredTime &&
    _flow.selectedDay === _flow.offeredDay &&
    _flow.selectedTime === _flow.offeredTime
  );
}
function showDtError(msg) {
  const err = document.getElementById("dt-error");
  if (err) { err.textContent = msg || ""; err.classList.remove("hidden"); }
}
function hideDtError() {
  const err = document.getElementById("dt-error");
  if (err) { err.textContent = ""; err.classList.add("hidden"); }
}
// datetime 화면 오른쪽 위에 제시된 목표 날짜/시간 표시
function updateDtTargetDisplay() {
  const target = document.getElementById("dt-target");
  if (!target) return;
  if (_flow.offeredDay && _flow.offeredTime) {
    target.innerHTML = `📅 <strong>${_flow.offeredDay}</strong>&nbsp;&nbsp;🕒 <strong>${_flow.offeredTime}</strong>`;
    target.classList.remove("hidden");
  } else {
    target.classList.add("hidden");
  }
}
function renderDateList() {
  const dayList = document.getElementById("dt-day-list");
  const timeList = document.getElementById("dt-time-list");
  const nextBtn = document.getElementById("btn-dt-next");
  if (!dayList) return;
  // 오늘 기준 5일짜리 공연 날짜 생성
  const days = [];
  const base = new Date();
  const wdNames = ["일", "월", "화", "수", "목", "금", "토"];
  for (let i = 0; i < 5; i++) {
    const d = new Date(base.getFullYear(), base.getMonth(), base.getDate() + i);
    const label = `${d.getMonth() + 1}월 ${d.getDate()}일 (${wdNames[d.getDay()]})`;
    days.push(label);
  }
  dayList.innerHTML = days.map((l) => {
    const cls = ["dt-option"];
    if (_flow.selectedDay === l) cls.push("selected");
    return `<button type="button" class="${cls.join(" ")}" data-val="${l}">${l}</button>`;
  }).join("");
  dayList.querySelectorAll(".dt-option").forEach(btn => {
    btn.addEventListener("click", () => {
      const val = btn.dataset.val;
      if (_flow.offeredDay && val !== _flow.offeredDay) {
        // 목표 날짜가 아니면 다음 단계로 못 간다.
        showNotTargetPopup();
        return;
      }
      hideDtError();
      _flow.selectedDay = val;
      renderDateList();
      renderTimeList();
    });
  });
  // 빈 토큰을 add 하면 DOMTokenList가 예외를 던지므로 false/true로 명시
  if (timeList) timeList.classList.toggle("hidden", !_flow.selectedDay);
  if (nextBtn) nextBtn.classList.toggle("hidden", !dtSelectionComplete());
}

function renderTimeList() {
  const timeList = document.getElementById("dt-time-list");
  const nextBtn = document.getElementById("btn-dt-next");
  if (!timeList) return;
  const times = OFFERED_TIMES; // 상수와 동일한 4개 시간
  timeList.classList.remove("hidden");
  timeList.innerHTML = times.map(t => {
    const cls = ["dt-option"];
    if (_flow.selectedTime === t) cls.push("selected");
    return `<button type="button" class="${cls.join(" ")}" data-val="${t}">${t} 공연</button>`;
  }).join("");
  timeList.querySelectorAll(".dt-option").forEach(btn => {
    btn.addEventListener("click", () => {
      const val = btn.dataset.val;
      if (_flow.offeredTime && val !== _flow.offeredTime) {
        // 목표 시간이 아니면 다음 단계로 못 간다.
        showNotTargetPopup();
        return;
      }
      hideDtError();
      _flow.selectedTime = val;
      renderTimeList();
    });
  });
  if (nextBtn) nextBtn.classList.toggle("hidden", !dtSelectionComplete());
}

// ---------- 좌석 선택 (기존 concert-seat-area) ----------
function enterSeatStep(zoneId) {
  // 매진 연출 기준 시각: 정각 12시(_clockOpenAt). 12시 이전 진입이면 0초로 취급.
  _seatEnterAt = (_clockOpenAt && Date.now() > _clockOpenAt) ? _clockOpenAt : (Date.now() - 1e9);
  _flow.step = "seats";
  hideClockAndQueueOverlays();
  showFlowScreen("screen-concert");
  const homeArea = document.getElementById("concert-home-area");
  const seatArea = document.getElementById("concert-seat-area");
  if (homeArea) homeArea.classList.add("hidden");
  if (seatArea) seatArea.classList.remove("hidden");
  renderConcertSeats(); // 기존 좌석 맵 렌더링
}

// ---------- 단계 5: 결제 (마지막) ----------
function enterPaymentStep() {
  _flow.step = "payment";
  const summary = document.getElementById("payment-summary");
  if (summary) {
    const nameEl = document.querySelector("[data-key=\"concert.name\"]");
    const priceEl = document.querySelector("[data-key=\"concert.price\"]");
    const si = _flow.selectedSeatInfo;
    const zoneLabel = si
      ? `${si.section}구역 ${si.floor}층`
      : (_flow.selectedZone || "-");
    const seatLabel = si
      ? `${si.row}열 ${si.seatNo}석`
      : "";
    // 좌석별 가격 — 선택 좌석의 구역·열·중앙 여부로 산정 (getSeatPrice)
    const seatPrice = si ? getSeatPrice(si) : null;
    const priceText = seatPrice != null
      ? seatPrice.toLocaleString("ko-KR")
      : (priceEl ? priceEl.textContent : "-");
    summary.innerHTML =
      `<div class="pay-row"><span>공연명</span><strong>${nameEl ? nameEl.textContent : "-"}</strong></div>` +
      `<div class="pay-row"><span>날짜</span><strong>${_flow.selectedDay || "-"}</strong></div>` +
      `<div class="pay-row"><span>시간</span><strong>${_flow.selectedTime || "-"}</strong></div>` +
      `<div class="pay-row"><span>구역</span><strong>${zoneLabel}</strong></div>` +
      (seatLabel ? `<div class="pay-row"><span>좌석</span><strong>${seatLabel}</strong></div>` : "") +
      `<div class="pay-row total"><span>결제 금액</span><strong>${priceText}원</strong></div>`;
  }
  showFlowScreen("screen-payment");
}

// 좌석 확정 → 결제로. (btn-concert-reset 초기화 버튼은 그대로 유지)
(function bindPaymentWiring() {
  // --- 보안문자 단계 ---
  const capNext = document.getElementById("btn-captcha-next");
  if (capNext) capNext.addEventListener("click", checkCaptcha);
  const capRefresh = document.getElementById("btn-captcha-refresh");
  if (capRefresh) capRefresh.addEventListener("click", () => enterCaptchaStep());
  const capInput = document.getElementById("captcha-input");
  if (capInput) capInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") checkCaptcha();
  });

  // --- 날짜/시간 단계 ---
  const dtBack = document.getElementById("btn-dt-back");
  if (dtBack) dtBack.addEventListener("click", () => enterCaptchaStep());
  const dtNext = document.getElementById("btn-dt-next");
  if (dtNext) dtNext.addEventListener("click", () => {
    hideNotTargetPopup();
    enterSeatStep(null);
  });
  const notTargetClose = document.getElementById("btn-not-target-close");
  if (notTargetClose) notTargetClose.addEventListener("click", hideNotTargetPopup);

  // --- 좌석 선택 단계: 좌석을 고르면 결제로 ---
  const seatMap = document.getElementById("section-seat-grid");
  if (seatMap) {
    seatMap.addEventListener("click", (e) => {
      const seat = e.target.closest("[data-seat]");
      if (!seat || seat.classList.contains("taken")) return;
      enterPaymentStep();
    });
  }

  // --- 결제 단계 ---
  const payBack = document.getElementById("btn-pay-back");
  if (payBack) payBack.addEventListener("click", () => enterSeatStep(_flow.selectedZone));
  const payConfirm = document.getElementById("btn-pay-confirm");
  if (payConfirm) payConfirm.addEventListener("click", () => {
    // 점수 계산
    const score = calcSeatScore(_flow.selectedSeatInfo);
    const si = _flow.selectedSeatInfo;
    const seatLine = si ? `${si.section}구역 ${si.floor}층 ${si.row}열` : (_flow.selectedZone || "-");
    const lines = [seatLine];
    showResult({ title: "예매 완료!", lines, score: score ? score.total : null });
  });
})();



function renderConfigValues() {
  const c = CONFIG.concert;
  const map = {
    "concert.name": c.name, "concert.totalSeats": (function() {
      let minSlot = Infinity;
      c.sections.forEach(sec => {
        if (sec.id === "A") return;
        const xs = sec.polygon.map(p => p[0]), ys = sec.polygon.map(p => p[1]);
        const uMax = Math.max(...xs), uMin = Math.min(...xs);
        const vMax = Math.max(...ys), vMin = Math.min(...ys);
        const aspect = (uMax - uMin) / Math.max(vMax - vMin, 0.001);
        let cols = Math.max(1, Math.round(Math.sqrt(sec.seats * aspect)));
        let rowsG = Math.max(1, Math.round(sec.seats / cols));
        for (let d = 0; d <= 6 && rowsG > 1; d++) {
          if (rowsG * cols >= sec.seats) break;
          cols++; rowsG = Math.max(1, Math.round(sec.seats / cols));
        }
        const s = Math.min((uMax - uMin) / cols, (vMax - vMin) / rowsG);
        if (s < minSlot) minSlot = s;
      });
      let total = 0;
      c.sections.forEach(sec => {
        const xs = sec.polygon.map(p => p[0]), ys = sec.polygon.map(p => p[1]);
        const uMax = Math.max(...xs), uMin = Math.min(...xs);
        const vMax = Math.max(...ys), vMin = Math.min(...ys);
        const aspect = (uMax - uMin) / Math.max(vMax - vMin, 0.001);
        let cols = Math.max(1, Math.round(Math.sqrt(sec.seats * aspect)));
        let rowsG = Math.max(1, Math.round(sec.seats / cols));
        for (let d = 0; d <= 6 && rowsG > 1; d++) {
          if (rowsG * cols >= sec.seats) break;
          cols++; rowsG = Math.max(1, Math.round(sec.seats / cols));
        }
        const fitCols = Math.max(1, Math.min(cols, Math.floor((uMax - uMin) / minSlot)));
        const fitRows = Math.max(1, Math.min(rowsG, Math.floor((vMax - vMin) / minSlot)));
        total += fitCols * fitRows;
      });
      return total;
    })(), "concert.price": fmt(c.price),
  };
  document.querySelectorAll(".config-val").forEach(el => {
    const key = el.dataset.key;
    if (map[key] !== undefined) el.textContent = map[key];
  });
}

function fmt(n) { return n ? n.toLocaleString() + "원" : "[수치 대기]"; }

// ============================================================
// 좌석 선점 점수 계산 — 구역 30 + 열 40 + 중앙 30 = 최대 100점
// ============================================================
function calcSeatScore(seatInfo) {
  if (!seatInfo) return null;
  const { section, floor, row, seatNo, cols } = seatInfo;

  // --- 구역 점수 (최대 30점) ---
  const zoneScores = { A: 30, B: 20, C: 20, D: 25, E: 25, F: 15, G: 15, H: 10, I: 10 };
  const zoneScore = zoneScores[section] || 0;

  // --- 열 점수 (최대 40점) ---
  let rowScore = 0;
  if (floor === 1) {
    if (row <= 3)       rowScore = 40;
    else if (row <= 6)  rowScore = 36;
    else if (row <= 10) rowScore = 31;
    else if (row <= 14) rowScore = 25;
    else                rowScore = 15;
  } else {
    if (row <= 3)       rowScore = 33;
    else if (row <= 6)  rowScore = 24;
    else if (row <= 10) rowScore = 14;
    else                rowScore = 8;
  }

  // --- 중앙 점수 (최대 30점) ---
  // 좌석 번호 기준, 구역 중앙 좌석부터 양쪽으로 몇 칸 떨어져 있는지 계산
  const centerPos = Math.ceil(cols / 2);
  const distFromCenter = Math.abs(seatNo - centerPos);
  let centerScore = 0;
  if (distFromCenter <= 10)      centerScore = 30;
  else if (distFromCenter <= 17) centerScore = 22;
  else if (distFromCenter <= 25) centerScore = 14;
  else                           centerScore = 8;

  return {
    zoneScore,
    rowScore,
    centerScore,
    total: zoneScore + rowScore + centerScore,
  };
}

// ============================================================
// 좌석별 결제 금액 — calcSeatScore()의 총점(최대 100)을 기준으로
// 5개 가격대로 나누어 매긴다.
//   90~100 → 250,000원 (좋은 구역 + 앞자리 + 중앙)
//   75~89  → 200,000원
//   60~74  → 160,000원
//   45~59  → 130,000원
//   그 이하 →  90,000원
// ============================================================
function getSeatPrice(seatInfo) {
  const score = calcSeatScore(seatInfo);
  if (!score) return null;
  if (score.total >= 90) return 250000;
  if (score.total >= 75) return 200000;
  if (score.total >= 60) return 160000;
  if (score.total >= 45) return 130000;
  return 90000;
}

// ---------- 좌석 맵 생성 ----------
function initScreen(mode) {
  if (mode === "concert") renderConcertSeats();
}

// ============================================================
// 아레나 좌석 맵 — 도면 형태
// 외곽 팔각형 + 중앙 무대(J) + 9개 구역(A~I)이 원호를 따라
// 개별 좌석 점(dot)으로 채워진 한 장의 도면.
// 구역 클릭 시 별도 화면(screen-section)으로 이동.
// ============================================================
const _sectionTaken = {}; // { [secId]: Set(좌석 번호) }
let _currentSection = null; // 현재 선택된 구역 id
let _globalMinSlot = Infinity; // 모든 구역 중 가장 작은 slot — 전 구역이 이 값으로 좌석을 그려 크기 통일

function renderConcertSeats() {
  const container = document.getElementById("concert-seat-map");
  container.innerHTML = "";
  const { sections } = CONFIG.concert;

  const map = document.createElement("div");
  map.className = "arena-map";

  // 전체 맵을 하나의 SVG로 렌더링 — 퍼즐 블록 형태
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("class", "arena-puzzle-svg");
  svg.setAttribute("viewBox", "-6 -2 112 84");
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");

  // 배경
  const bg = document.createElementNS(NS, "rect");
  bg.setAttribute("x", "0"); bg.setAttribute("y", "0");
  bg.setAttribute("width", "100"); bg.setAttribute("height", "82");
  bg.setAttribute("fill", "#1a1a2e");
  bg.setAttribute("rx", "3");
  svg.appendChild(bg);

  // 9개 구역 — 퍼즐처럼 맞물리는 polygon 블록
  // _globalMinSlot 초기화: 각 구역 rawSlot을 미리 계산해 최소값 확정
  // y축 단일 스케일: raw 18~76 → 4~76 (스케일 1.25).
  // raw에서 D/E/F/G/H/I 모두 16 단위 높이 → 화면에서도 20으로 동일.
  const remapY = (y) => 4 + (y - 18) * 1.25;
  {
    let minSlot = Infinity;
    sections.forEach(sec => {
      const xs = sec.polygon.map(p => p[0]), ys = sec.polygon.map(p => remapY(p[1]));
      const uMax = Math.max(...xs), uMin = Math.min(...xs);
      const vMax = Math.max(...ys), vMin = Math.min(...ys);
      const aspect = (uMax - uMin) / Math.max(vMax - vMin, 0.001);
      const totalSeats = sec.seats;
      let cols = Math.max(1, Math.round(Math.sqrt(totalSeats * aspect)));
      let rowsG = Math.max(1, Math.round(totalSeats / cols));
      for (let d = 0; d <= 6 && rowsG > 1; d++) {
        if (rowsG * cols >= totalSeats) break;
        cols++;
        rowsG = Math.max(1, Math.round(totalSeats / cols));
      }
      if (sec.id !== "A") {
        const s = Math.min((uMax - uMin) / cols, (vMax - vMin) / rowsG);
        if (s < minSlot) minSlot = s;
      }
    });
    _globalMinSlot = minSlot;
  }
  // 각 구역의 실제 좌석 수 — _globalMinSlot 기반 격자 배치가 가능한 cols/rows를 계산해
  // cols*rows (실제 렌더링되는 좌석 수)를 actualSeatCount로 사용.
  function computeActualSeatCount(sec) {
    const xs = sec.polygon.map(p => p[0]), ys = sec.polygon.map(p => remapY(p[1]));
    const uMax = Math.max(...xs), uMin = Math.min(...xs);
    const vMax = Math.max(...ys), vMin = Math.min(...ys);
    const aspect = (uMax - uMin) / Math.max(vMax - vMin, 0.001);
    const totalSeats = sec.seats;
    let cols = Math.max(1, Math.round(Math.sqrt(totalSeats * aspect)));
    let rowsG = Math.max(1, Math.round(totalSeats / cols));
    for (let d = 0; d <= 6 && rowsG > 1; d++) {
      if (rowsG * cols >= totalSeats) break;
      cols++;
      rowsG = Math.max(1, Math.round(totalSeats / cols));
    }
    // A구역은 전역 최소 slot 기준에서 제외되므로 별도 처리 없음
    // 모든 구역: _globalMinSlot을 기준으로 배치 가능한 최대 좌석 수 계산
    const slot = _globalMinSlot;
    const maxColsByU = Math.floor((uMax - uMin) / slot);
    const maxRowsByV = Math.floor((vMax - vMin) / slot);
    const fitCols = Math.max(1, Math.min(cols, maxColsByU));
    const fitRows = Math.max(1, Math.min(rowsG, maxRowsByV));
    return fitCols * fitRows;
  }

  let _sumActual = 0;
  sections.forEach(sec => {
    const actualSeatCount = computeActualSeatCount(sec);
    _sumActual += actualSeatCount;
    const pts = sec.polygon.map(p => `${p[0]},${remapY(p[1])}`).join(" ");
    const poly = document.createElementNS(NS, "polygon");
    poly.setAttribute("points", pts);
    poly.setAttribute("fill", sec.color);
    poly.setAttribute("class", "puzzle-block");
    poly.setAttribute("data-sec", sec.id);
    poly.style.cursor = "pointer";
    poly.style.transition = "opacity 0.15s, filter 0.15s";

    poly.addEventListener("mouseenter", () => {
      poly.style.filter = "brightness(1.25)";
      poly.style.opacity = "0.9";
    });
    poly.addEventListener("mouseleave", () => {
      poly.style.filter = "";
      poly.style.opacity = "";
    });
    poly.addEventListener("click", () => openSectionScreen(sec.id));

    const tip = document.createElementNS(NS, "title");
    tip.textContent = `${sec.name} — ${actualSeatCount.toLocaleString()}석`;
    poly.appendChild(tip);
    svg.appendChild(poly);

    // 구역 라벨
    const [lx, lyRaw] = sec.labelPos;
    const ly = remapY(lyRaw);
    const text = document.createElementNS(NS, "text");
    text.setAttribute("x", lx);
    text.setAttribute("y", ly);
    text.setAttribute("text-anchor", "middle");
    text.setAttribute("dominant-baseline", "central");
    text.setAttribute("fill", "#fff");
    text.setAttribute("font-size", "4.5");
    text.setAttribute("font-weight", "bold");
    text.setAttribute("pointer-events", "none");
    text.textContent = sec.id;
    svg.appendChild(text);

    // 좌석 수 표시
    const seatText = document.createElementNS(NS, "text");
    seatText.setAttribute("x", lx);
    seatText.setAttribute("y", ly + 4.5);
    seatText.setAttribute("text-anchor", "middle");
    seatText.setAttribute("dominant-baseline", "central");
    seatText.setAttribute("fill", "rgba(255,255,255,0.7)");
    seatText.setAttribute("font-size", "2.2");
    seatText.setAttribute("pointer-events", "none");
    seatText.textContent = actualSeatCount.toLocaleString() + "석";
    svg.appendChild(seatText);
  });
  _actualTotalSeats = _sumActual;

  // 무대 (STAGE) — 중앙, 단색
  const stageCfg = CONFIG.concert.stage;
  const stageW = stageCfg.w, stageH = stageCfg.h;
  const sx = stageCfg.cx - stageW / 2, sy = stageCfg.cy - stageH / 2;
  const stageRect = document.createElementNS(NS, "rect");
  stageRect.setAttribute("x", sx);
  stageRect.setAttribute("y", sy);
  stageRect.setAttribute("width", stageW);
  stageRect.setAttribute("height", stageH);
  stageRect.setAttribute("fill", "#e8c84a");
  stageRect.setAttribute("stroke", "#b89830");
  stageRect.setAttribute("stroke-width", "0.5");
  stageRect.setAttribute("rx", "2");
  svg.appendChild(stageRect);

  const stageLabel = document.createElementNS(NS, "text");
  stageLabel.setAttribute("x", 50);
  stageLabel.setAttribute("y", sy + stageH / 2);
  stageLabel.setAttribute("text-anchor", "middle");
  stageLabel.setAttribute("dominant-baseline", "central");
  stageLabel.setAttribute("fill", "#1a1a2e");
  stageLabel.setAttribute("font-size", "3.5");
  stageLabel.setAttribute("font-weight", "bold");
  stageLabel.setAttribute("letter-spacing", "0.5");
  stageLabel.textContent = "무 대";
  svg.appendChild(stageLabel);

  map.appendChild(svg);
  container.appendChild(map);
}

// ============================================================
// 구역 클릭 → 별도 화면으로 이동
// 좌석은 해당 구역 polygon 모양을 따라 배치 + 무대 방향 화살표 표기
// 좌표계: (u,v) — u = 무대 수직 축, v = 무대 방향 축, 원점 = 구역 중심
// ============================================================

// 무대 방향 + 좌표계 변환 매트릭스 계산
function computeSectionFrame(sec) {
  const pts = sec.polygon;
  let cx = 0, cy = 0;
  pts.forEach(p => { cx += p[0]; cy += p[1]; });
  cx /= pts.length; cy /= pts.length;
  const stage = CONFIG.concert.stage;
  const dx = stage.cx - cx, dy = stage.cy - cy;
  const len = Math.sqrt(dx * dx + dy * dy) || 1;
  const ux = dx / len, uy = dy / len;   // 무대 방향 단위벡터 (v축)
  const px = -uy, py = ux;              // 수직 단위벡터 (u축)
  return { cx, cy, ux, uy, px, py };
}

// (x,y) → (u,v): u = 수직축, v = 무대방향축, 원점 = (cx,cy)
function toUV(x, y, f) {
  const rx = x - f.cx, ry = y - f.cy;
  return [rx * f.px + ry * f.py, rx * f.ux + ry * f.uy];
}

// (u,v) → (x,y)
function fromUV(u, v, f) {
  return [f.cx + u * f.px + v * f.ux, f.cy + u * f.py + v * f.uy];
}

// u = uPrime인 직선이 polygon 변들과 교차하는 v 값 목록
function uvCrossings(pts, uPrime, f) {
  const uv = pts.map(p => toUV(p[0], p[1], f));
  const hits = [];
  for (let i = 0; i < uv.length; i++) {
    const a = uv[i], b = uv[(i + 1) % uv.length];
    if ((a[0] <= uPrime && b[0] >= uPrime) || (b[0] <= uPrime && a[0] >= uPrime)) {
      const t = (uPrime - a[0]) / (b[0] - a[0]);
      hits.push(a[1] + t * (b[1] - a[1]));
    }
  }
  return hits;
}

// 정각(공연 시작) 기준 시각 — 스위치 off 상태에서 '좌석 선택' 단계 진입 시점에 기록
var _seatEnterAt = null;

// ---------- 입장 경과 시간 → 매진 연출 시나리오 ----------
// 4가지 시나리오는 남은 좌석 수(전체 합)만 결정하고,
// "어느 열에 무엇을 남길지"는 openSectionScreen의 BANDS가 담당한다.
const SCENARIOS = [
  { until: 180, remaining: null },   // 3분 내: 5000석 매진 (TOTAL - 5000)
  { until: 300, remaining: 5000 },   // 3분 초과 ~ 5분: 5000석 남음
  { until: 480, remaining: 3000 },   // 5분 초과 ~ 8분: 3000석 남음
  { until: 600, remaining: 1000 },   // 8분 초과 ~ 10분: 1000석 남음
];

function getScenario() {
  if (!_seatEnterAt) return null;
  const elapsedSec = (Date.now() - _seatEnterAt) / 1000;
  for (const s of SCENARIOS) {
    if (elapsedSec <= s.until) return s;
  }
  return null; // 10분 초과: 기존 잔여량 로직 사용
}

function remainingSeatsByElapsed() {
  // 잔여 좌석 총량(모든 구역 합) 기준: 약 20,059석 규모 가정
  const TOTAL = 20059;
  if (!_seatEnterAt) return TOTAL;
  const sc = getScenario();
  if (sc) {
    return sc.remaining === null ? TOTAL - 5000 : sc.remaining;
  }
  const elapsedSec = (Date.now() - _seatEnterAt) / 1000;
  let min, max;
  if (elapsedSec < 600) { min = 0; max = 1000; }
  else                  { min = 0; max = 0; } // 10분 이후 계속 매진
  return Math.floor(min + Math.random() * (max - min + 1));
}

// ---------- 우측 상단 전체 구역표 미니맵 ----------
// 첨부 이미지(43.png) 모양: 9개 섹션 A~I가 3×3 격자로 배치되고
// 중앙 셀에 무대(J)가 들어간다. 각 섹션은 polygon labelPos로 결정된
// 실제 도면 위치와 동일한 격자 칸에 놓인다.
//
//   ┌─────┬─────┬─────┐
//   │  B  │  A  │  C  │   row 0: y < 32%
//   ├─────┼─────┼─────┤
//   │  D  │ 무대│  E  │   row 1: 32% ≤ y < 64%
//   ├─────┼─────┼─────┤
//   │  F  │     │  G  │   row 2: 64% ≤ y < 80%  (F,G만 존재)
//   ├─────┼─────┼─────┤
//   │  H  │     │  I  │   row 3: y ≥ 80%        (H,I만 존재)
//   └─────┴─────┴─────┘
const MINIMAP_CELLS = {
  A: { col: 2, row: 1 },
  B: { col: 1, row: 1 },
  C: { col: 3, row: 1 },
  D: { col: 1, row: 2 },
  E: { col: 3, row: 2 },
  F: { col: 1, row: 3 },
  G: { col: 3, row: 3 },
  H: { col: 1, row: 4 },
  I: { col: 3, row: 4 },
};
const MINIMAP_STAGE_CELL = { col: 2, row: 2 };

function renderSectionMinimap() {
  const grid = document.getElementById("minimap-grid");
  if (!grid) return;
  const sections = CONFIG.concert.sections || [];
  grid.innerHTML = "";

  // 각 섹션 버튼을 지정된 격자 칸에 배치
  sections.forEach((s) => {
    const cell = MINIMAP_CELLS[s.id];
    if (!cell) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "minimap-cell minimap-seat" + (s.id === _currentSection ? " active" : "");
    btn.dataset.zone = s.id;
    btn.style.gridColumn = cell.col;
    btn.style.gridRow = cell.row;
    btn.textContent = s.id;
    btn.title = `${s.name || s.id} · ${s.seats.toLocaleString()}석`;
    btn.addEventListener("click", () => {
      if (s.id === _currentSection) return;
      openSectionScreen(s.id);
    });
    grid.appendChild(btn);
  });

  // 중앙 무대 셀
  const stage = document.createElement("div");
  stage.className = "minimap-cell minimap-stage";
  stage.style.gridColumn = MINIMAP_STAGE_CELL.col;
  stage.style.gridRow = MINIMAP_STAGE_CELL.row;
  stage.textContent = "무대";
  grid.appendChild(stage);
}

function openSectionScreen(secId) {
  const sec = CONFIG.concert.sections.find(s => s.id === secId);
  if (!sec) return;
  _currentSection = secId;

  document.querySelectorAll(".mode-screen").forEach(s => s.classList.remove("active"));
  document.getElementById("screen-section").classList.add("active");
  document.getElementById("section-title").textContent = `${sec.name} 좌석 선택`;
  document.getElementById("section-info").innerHTML =
    `좌석 수: <b>${sec.seats.toLocaleString()}석</b> &nbsp;|&nbsp; 가격: <b>${fmt(sec.price)}</b>`;

  renderSectionMinimap(); // 우측 상단 전체 구역표 갱신 (현재 구역 하이라이트)

  const grid = document.getElementById("section-seat-grid");
  grid.innerHTML = "";
  const taken = _sectionTaken[secId] || new Set();

  // ---- 좌석 격자 크기 계산 (아래 매진 처리와 렌더링에서 사용) ----
  const f0 = computeSectionFrame(sec);
  const uvPts0 = sec.polygon.map(p => toUV(p[0], p[1], f0));
  const uMin0 = Math.min(...uvPts0.map(p => p[0])), uMax0 = Math.max(...uvPts0.map(p => p[0]));
  const vMin0 = Math.min(...uvPts0.map(p => p[1])), vMax0 = Math.max(...uvPts0.map(p => p[1]));
  const aspect0b = (uMax0 - uMin0) / Math.max(vMax0 - vMin0, 0.001);
  let cols = Math.max(1, Math.round(Math.sqrt(sec.seats * aspect0b)));
  let rowsG = Math.max(1, Math.round(sec.seats / cols));
  for (let d = 0; d <= 6 && rowsG > 1; d++) {
    if (rowsG * cols >= sec.seats) break;
    cols++;
    rowsG = Math.max(1, Math.round(sec.seats / cols));
  }
  const slot = _globalMinSlot;
  const seatSize = slot * (1 - 0.25);
  const seatW = seatH = seatSize;
  const actualSeatCount = cols * rowsG;

  // ---- 입장 경과 시간대별 매진 연출 ----
  // row = 무대와의 거리(row 0 = 무대 바로 앞). 1층/2층 경계는 전체 행의 60% 지점.
  // 시나리오별로 "열당 남은 좌석 비율"을 지정하고, 그 비율만큼만 살아 있게 둔다.
  // (매진 = 나머지를 taken 처리.) 비율 합계는 대략 100%가 되도록 설계되어
  // 시나리오의 총량(5000/3000/1000석 남음)과 일치한다.
  const SCENARIO_BANDS = [
    // ① 3분 내: 약 5000석 매진 → 아래 열들이 매진되는 비율
    [
      { f: 1, from: 1,  to: 3,  sold: 0.93 },
      { f: 1, from: 4,  to: 5,  sold: 0.04 },
      { f: 1, from: 6,  to: 8,  sold: 0.01 },
      { f: 2, from: 1,  to: 2,  sold: 0.01 },
      { f: 2, from: 3,  to: 5,  sold: 0.005 },
      { f: 2, from: 6,  to: null, sold: 0.005 },
    ],
    // ② 3분 초과 ~ 5분: 약 5000석 남음
    [
      { f: 1, from: 1,  to: 3,  remain: 0 },
      { f: 1, from: 4,  to: 5,  remain: 0.04 },
      { f: 1, from: 6,  to: 8,  remain: 0.07 },
      { f: 1, from: 9,  to: 11, remain: 0.12 },
      { f: 1, from: 12, to: 14, remain: 0.15 },
      { f: 1, from: 15, to: null, remain: 0.18 },
      { f: 2, from: 1,  to: 2,  remain: 0.04 },
      { f: 2, from: 3,  to: 5,  remain: 0.08 },
      { f: 2, from: 6,  to: null, remain: 0.22 },
    ],
    // ③ 5분 초과 ~ 8분: 약 3000석 남음
    [
      { f: 1, from: 1,  to: 5,  remain: 0 },
      { f: 1, from: 6,  to: 8,  remain: 0.07 },
      { f: 1, from: 9,  to: 11, remain: 0.13 },
      { f: 1, from: 12, to: 14, remain: 0.17 },
      { f: 1, from: 15, to: null, remain: 0.21 },
      { f: 2, from: 1,  to: 2,  remain: 0 },
      { f: 2, from: 3,  to: 5,  remain: 0.08 },
      { f: 2, from: 6,  to: null, remain: 0.24 },
    ],
    // ④ 8분 초과 ~ 10분: 약 1000석 남음
    [
      { f: 1, from: 1,  to: 8,  remain: 0 },
      { f: 1, from: 9,  to: 11, remain: 0.10 },
      { f: 1, from: 12, to: 14, remain: 0.18 },
      { f: 1, from: 15, to: null, remain: 0.25 },
      { f: 2, from: 1,  to: 5,  remain: 0 },
      { f: 2, from: 6,  to: 9,  remain: 0.22 },
      { f: 2, from: 10, to: null, remain: 0.25 },
    ],
  ];

  if (_seatEnterAt && !_sectionTaken[secId] && cols > 0 && rowsG > 0) {
    const sc = getScenario();
    if (sc) {
      const bandIdx = SCENARIOS.indexOf(sc);
      const bands = SCENARIO_BANDS[bandIdx];
      // 1층/2층 경계: 전체 행의 60% (기존 isFloor2 기준과 동일)
      const floor1Rows = Math.max(1, Math.ceil(rowsG * 0.6));
      const floor2Start = floor1Rows; // 0-based
      const floor2Rows = Math.max(0, rowsG - floor2Start);

      // 각 열(row)마다 남은 비율을 결정 → 그 비율만큼만 공석으로 둔다.
      for (let r = 0; r < rowsG; r++) {
        const isF1 = r < floor2Start;
        const relRow = isF1 ? (r + 1) : (r - floor2Start + 1); // 해당 층 내 1-based 열 번호
        let remainRatio = 1; // 기본: 전부 남김
        for (const b of bands) {
          const inFloor = isF1 ? b.f === 1 : b.f === 2;
          if (!inFloor) continue;
          const lo = b.from;
          const hi = (b.to === null) ? Infinity : b.to;
          if (relRow >= lo && relRow <= hi) {
            remainRatio = (b.remain !== undefined) ? b.remain : (1 - b.sold);
            break;
          }
        }
        // 이 열의 좌석 중 remainRatio 비율만 공석, 나머지는 매진
        const seatCountInRow = cols;
        const keepCount = Math.round(seatCountInRow * remainRatio);
        if (keepCount >= seatCountInRow) continue; // 전부 남김
        if (keepCount <= 0) {
          // 전부 매진
          for (let c = 0; c < cols; c++) {
            const idx = r * cols + c + 1;
            if (!taken.has(idx)) taken.add(idx);
          }
        } else {
          // keepCount개만 공석으로 남기고 나머지 매진 —
          // 어느 자리가 남을지는 랜덤(열 안 섞임)
          const order = [];
          for (let c = 0; c < cols; c++) order.push(c);
          // Fisher-Yates 셔플
          for (let i = order.length - 1; i > 0; i--) {
            const j = Math.floor(Math.random() * (i + 1));
            [order[i], order[j]] = [order[j], order[i]];
          }
          const kept = new Set(order.slice(0, keepCount));
          for (let c = 0; c < cols; c++) {
            if (kept.has(c)) continue;
            const idx = r * cols + c + 1;
            if (!taken.has(idx)) taken.add(idx);
          }
        }
      }
      _sectionTaken[secId] = taken;
    } else {
      // 10분 초과: 기존 잔여량 기반 매진 (전체 frac 비율 적용)
      const totalAllSections = CONFIG.concert.sections.reduce((s, x) => s + x.seats, 0);
      const remainingTotal = Math.min(totalAllSections, remainingSeatsByElapsed());
      const frac = totalAllSections ? remainingTotal / totalAllSections : 0;
      const remainingHere = Math.floor(sec.seats * frac);
      const seatsToTake = Math.max(0, actualSeatCount - remainingHere);
      if (seatsToTake > 0) {
        // 앞쪽 열에 매진이 몰리도록 구간별 비율로 배정
        const bands = [
          { from: 1,  to: 10, ratio: 0.70 },
          { from: 11, to: 15, ratio: 0.15 },
          { from: 16, to: 20, ratio: 0.10 },
          { from: 21, to: null, ratio: 0.05 },
        ];
        const bandTargets = bands.map(b => {
          const lo = b.from - 1;
          const hi = (b.to === null ? rowsG : Math.min(b.to, rowsG)) - 1;
          const rowCount = Math.max(0, hi - lo + 1);
          return { lo, hi, count: rowCount, target: Math.round(seatsToTake * b.ratio) };
        });
        let assigned = bandTargets.reduce((s, b) => s + Math.min(b.target, b.count * cols), 0);
        let leftover = seatsToTake - assigned;
        for (const b of [...bandTargets].sort((x, y) => y.target - x.target)) {
          if (leftover <= 0) break;
          const room = b.count * cols - Math.min(b.target, b.count * cols);
          const add = Math.min(room, leftover);
          b.target += add;
          leftover -= add;
        }
        for (const b of bandTargets) {
          if (b.target <= 0 || b.count <= 0) continue;
          const want = Math.min(b.target, b.count * cols);
          let need = want;
          const maxTries = need * 20 + 100;
          let tries = 0;
          while (need > 0 && tries < maxTries) {
            tries++;
            const r = b.lo + Math.floor(Math.random() * b.count);
            const c = Math.floor(Math.random() * cols);
            const idx = r * cols + c + 1;
            if (!taken.has(idx)) { taken.add(idx); need--; }
          }
        }
        _sectionTaken[secId] = taken;
      }
    }
  }
  const NS = "http://www.w3.org/2000/svg";
  const f = computeSectionFrame(sec);

  // UV 공간 bbox
  const uvPts = sec.polygon.map(p => toUV(p[0], p[1], f));
  const uMin = Math.min(...uvPts.map(p => p[0])), uMax = Math.max(...uvPts.map(p => p[0]));
  const vMin = Math.min(...uvPts.map(p => p[1])), vMax = Math.max(...uvPts.map(p => p[1]));

  const totalSeats = sec.seats;

  // viewBox: 좌석 bbox에 가까운 마진으로 축소 → 좌석이 크게 보임
  const xs = sec.polygon.map(p => p[0]), ys = sec.polygon.map(p => p[1]);
  // 좌석이 많으면 좌석 격자가 polygon bbox보다 커져서 viewBox를 넓혀야 함
  // → 양쪽/상하 여백을 늘려서 좌석이 화면 안에 들어옴
  const aspect0 = (Math.max(...xs) - Math.min(...xs)) / Math.max(Math.max(...ys) - Math.min(...ys), 0.001);
  let cols0 = Math.max(1, Math.round(Math.sqrt(totalSeats * aspect0)));
  let rowsG0 = Math.max(1, Math.round(totalSeats / cols0));
  for (let d = 0; d <= 6 && rowsG0 > 1; d++) {
    if (rowsG0 * cols0 >= totalSeats) break;
    cols0++;
    rowsG0 = Math.max(1, Math.round(totalSeats / cols0));
  }
  const slot0 = _globalMinSlot;
  const gridW0 = cols0 * slot0, gridH0 = rowsG0 * slot0;
  const polyW = Math.max(...xs) - Math.min(...xs), polyH = Math.max(...ys) - Math.min(...ys);
  // 좌석 격자가 polygon보다 넓은 경우(A구역 등 좌석 많은 구역):
  // 격자 중심을 polygon 중심과 일치시키고, 좌석 간격(slot)의 일정 배수만큼
  // 상하좌우 여백을 보장해 좌석이 화면 양 끝에 붙지 않고 넓게 보이게 한다.
  const MARGIN_SLOTS = 1.5; // 좌석 1칸 반 정도의 여백
  const marginX = slot0 * MARGIN_SLOTS;
  const marginY = slot0 * MARGIN_SLOTS;
  // polygon bbox와 좌석 격자 bbox 중 더 큰 쪽을 기준으로 viewBox 계산
  const contentW = Math.max(polyW, gridW0);
  const contentH = Math.max(polyH, gridH0);
  const cx = (Math.min(...xs) + Math.max(...xs)) / 2;
  const cy = (Math.min(...ys) + Math.max(...ys)) / 2;
  const vbX = cx - contentW / 2 - marginX;
  const vbY = cy - contentH / 2 - marginY;
  const vbW = contentW + marginX * 2;
  const vbH = contentH + marginY * 2;

  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", `${vbX} ${vbY} ${vbW} ${vbH}`);
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
  svg.style.width = "100%";
  svg.style.maxWidth = "1200px";
  svg.style.height = "auto";
  svg.style.display = "block";
  svg.style.margin = "0 auto";

  // ============================================================
  // 직사각형 격자 좌석 배치 (polygon 모양 무시)
  // - 좌석은 항상 완전한 직사각형 블록을 이룸
  // - 앞쪽 60% = 1층, 뒤쪽 40% = 2층
  // ============================================================
  // 좌석은 정사각형 + 상하좌우 동일한 간격
  // 좌석:간격 = 3:1 (slot의 75%가 좌석, 25%가 간격)
const GAP_RATIO = 0.25;
  const aspect = (uMax - uMin) / Math.max(vMax - vMin, 0.001);
  // ±100 범위에서 cols × rows가 정확히 맞는 직사각형 격자 찾기
  // config 좌석 수에서 벗어나지 않으면서 가장 가까운 완전 직사각형을 선택
  let bestCols = 0, bestRows = 0, bestDiff = Infinity;
  const minSeats = Math.max(1, totalSeats - 100);
  const maxSeats = totalSeats + 100;
  for (let c = 1; c <= Math.ceil(Math.sqrt(maxSeats)) + 20; c++) {
    // 이 열 수로 minSeats~maxSeats 범위에 들어가는 행 수 찾기
    const rMin = Math.ceil(minSeats / c);
    const rMax = Math.floor(maxSeats / c);
    if (rMin > rMax) continue;
    // aspect 비율에 가장 가까운 것 선택 (격자가 너무 길쭉하거나 짧지 않게)
    const targetAspect = aspect;
    let bestR = rMin;
    let bestRArr = Infinity;
    for (let r = rMin; r <= rMax; r++) {
      const a = c / r;
      const diff = Math.abs(a - targetAspect);
      if (diff < bestRArr) { bestRArr = diff; bestR = r; }
    }
    const total = c * bestR;
    const d = Math.abs(total - totalSeats);
    if (d < bestDiff) { bestDiff = d; bestCols = c; bestRows = bestR; }
  }
  // fallback: 찾지 못하면 기존 방식
  if (!bestCols) {
    bestCols = Math.max(1, Math.round(Math.sqrt(totalSeats * aspect)));
    bestRows = Math.max(1, Math.round(totalSeats / bestCols));
  }
  // cols/rowsG/slot/seatW/seatH/actualSeatCount는 함수 앞부분에서 이미 계산됨
  // (매진 처리 블록에서 먼저 사용하므로)
  // 격자를 구역 중앙에 정확히 배치 → 상하좌우 여백이 동일
  const gridW = cols * slot, gridH = rowsG * slot;
  const uOff = (uMax - uMin - gridW) / 2;
  const vOff = (vMax - vMin - gridH) / 2;
  const colSlot = slot;
  const rowSlot = slot;

  // 1층/2층 분할: 앞쪽 60% 행 = 1층, 나머지 40% = 2층
  const floor1Rows = Math.round(rowsG * 0.6);
  const FLOOR2_COLOR = "#5a5a7a";  // 2층 대표색 (어두운 남보라)

  // screen-space 좌표 변환 (axis-aligned)
  const polyXs = sec.polygon.map(p => p[0]), polyYs = sec.polygon.map(p => p[1]);
  const polyXMin = Math.min(...polyXs), polyXMax = Math.max(...polyXs);
  const polyYMin = Math.min(...polyYs), polyYMax = Math.max(...polyYs);
  function toXYAxis(u, v) {
    const tx = polyXMin + (u - uMin) / (uMax - uMin || 1) * (polyXMax - polyXMin);
    const ty = polyYMin + (v - vMin) / (vMax - vMin || 1) * (polyYMax - polyYMin);
    return [tx, ty];
  }

  // 좌석 그리기 — polygon 검사 없이 전체 직사각형 채움 (중앙 정렬 offset 반영)
  let seatIdx = 0;
  let renderedCount = 0;
  for (let r = 0; r < rowsG; r++) {
    const vPrime = vMin + uOff * 0 + vOff + (r + 0.5) * rowSlot;
    const isFloor2 = r >= floor1Rows;  // 앞쪽(floor1Rows 행) = 1층, 뒤쪽 = 2층
    const baseColor = isFloor2 ? FLOOR2_COLOR : sec.color;
    // 완전한 직사각형: 모든 행을 cols 만큼 채움 (±100 범위 내 실제 좌석 수)
    const seatsInRow = cols;
    for (let c = 0; c < seatsInRow; c++) {
      const uPrime = uMin + uOff + (c + 0.5) * colSlot;
      const [sx, sy] = toXYAxis(uPrime, vPrime);
      seatIdx++;
      renderedCount++;
      const isTaken = taken.has(seatIdx);
      const floorLabel = isFloor2 ? "2층" : "1층";

      const rect = document.createElementNS(NS, "rect");
      rect.setAttribute("x", (sx - seatW / 2).toFixed(3));
      rect.setAttribute("y", (sy - seatH / 2).toFixed(3));
      rect.setAttribute("width", seatW.toFixed(3));
      rect.setAttribute("height", seatH.toFixed(3));

      rect.setAttribute("fill", isTaken ? "#333" : baseColor);
      rect.setAttribute("stroke", "#1a1a2e");
      rect.setAttribute("stroke-width", "0.05");
      rect.setAttribute("class", "seat-rect" + (isTaken ? " taken" : "") + (isFloor2 ? " floor2" : " floor1"));
      rect.setAttribute("data-seat", String(seatIdx));
      rect.style.cursor = isTaken ? "not-allowed" : "pointer";
      rect.style.transition = "fill 0.1s";
      if (!isTaken) {
        rect.addEventListener("mouseenter", () => rect.setAttribute("fill", "#e94560"));
        rect.addEventListener("mouseleave", () => rect.setAttribute("fill", baseColor));
      }
      rect.addEventListener("click", () => {
        if (rect.classList.contains("taken")) return;
        // 12:00:00 정각 전에는 예매 불가 — 팝업으로 안내
        if (!_ticketingOpen) { showPreSalePopup(); return; }
        rect.classList.add("taken");
        rect.setAttribute("fill", "#4ecdc4");
        rect.style.cursor = "default";
        if (!taken.has(seatIdx)) { taken.add(seatIdx); _sectionTaken[secId] = taken; }
        // 점수 계산용 좌석 정보 저장
        _flow.selectedSeatInfo = {
          section: sec.id,
          floor: isFloor2 ? 2 : 1,
          row: r + 1,                    // 구역 내 행 (무대 가까운 쪽부터 1)
          seatNo: c + 1,                 // 행 내 좌석 번호 (왼쪽부터 1)
          cols: cols,                    // 구역의 총 열 수 (중앙 계산용)
          rowsG: rowsG,                  // 구역의 총 행 수
        };
        enterPaymentStep();
      });
      svg.appendChild(rect);
    }
  }

  // ---- 1층/2층 레이블: 좌석 격자 양쪽 바깥에 작게 세로 배치 ----
  // 좌석 격자 x 범위 직접 계산 (polygon bbox와 다를 수 있음 — uOff/vOff 중앙 정렬 때문)
  const gridLeftX = toXYAxis(uMin + uOff, vMin)[0];
  const gridRightX = toXYAxis(uMin + uOff + gridW, vMin)[0];
  // 레이블은 좌석 격자 양쪽 바깥에 수평(세로 회전 없음)으로 배치 — viewBox는 아래에서 레이블 폭만큼 넓힌다
  const sideOffset = Math.max(rowSlot * 0.7, 1.4);
  const leftLabelX = gridLeftX - sideOffset;
  const rightLabelX = gridRightX + sideOffset;
  const lbl1Y = toXYAxis(uMin, vMin + vOff + (floor1Rows / 2) * rowSlot)[1];
  const lbl2Y = toXYAxis(uMin, vMin + vOff + (floor1Rows + (rowsG - floor1Rows) / 2) * rowSlot)[1];
  [[leftLabelX, "1층"], [rightLabelX, "1층"]].forEach(([sx, txt]) => {
    const t = document.createElementNS(NS, "text");
    t.setAttribute("x", sx.toFixed(2));
    t.setAttribute("y", lbl1Y.toFixed(2));
    t.setAttribute("text-anchor", "middle");
    t.setAttribute("dominant-baseline", "central");
    t.setAttribute("fill", "#fff");
    t.setAttribute("font-size", "1.0");
    t.setAttribute("font-weight", "bold");
    t.setAttribute("opacity", "0.85");
    t.textContent = txt;
    svg.appendChild(t);
  });
  [[leftLabelX, "2층"], [rightLabelX, "2층"]].forEach(([sx, txt]) => {
    const t = document.createElementNS(NS, "text");
    t.setAttribute("x", sx.toFixed(2));
    t.setAttribute("y", lbl2Y.toFixed(2));
    t.setAttribute("text-anchor", "middle");
    t.setAttribute("dominant-baseline", "central");
    t.setAttribute("fill", "#cfcfe8");
    t.setAttribute("font-size", "1.0");
    t.setAttribute("font-weight", "bold");
    t.setAttribute("opacity", "0.85");
    t.textContent = txt;
    svg.appendChild(t);
  });

  // ---- 좌석 수 검증 (±100 허용 범위 내이면 정상) ----
  if (Math.abs(renderedCount - sec.seats) > 100) {
    console.warn(`[좌석 수 허용 범위 초과] ${sec.name}: config=${sec.seats}, rendered=${renderedCount}`);
  }

  // 좌석 영역이 실제로 차지한 bbox (viewBox 마진 밖으로 밀려나지 않도록)
  let seatMinX = Infinity, seatMinY = Infinity, seatMaxX = -Infinity, seatMaxY = -Infinity;
  svg.querySelectorAll(".seat-rect").forEach(el => {
    const x = parseFloat(el.getAttribute("x")), y = parseFloat(el.getAttribute("y"));
    const w = parseFloat(el.getAttribute("width")), h = parseFloat(el.getAttribute("height"));
    seatMinX = Math.min(seatMinX, x); seatMinY = Math.min(seatMinY, y);
    seatMaxX = Math.max(seatMaxX, x + w); seatMaxY = Math.max(seatMaxY, y + h);
  });
  if (!isFinite(seatMinX)) { seatMinX = vbX; seatMinY = vbY; seatMaxX = vbX + vbW; seatMaxY = vbY + vbH; }

  // ---- 무대 방향 표시: 좌석 영역 상단 중앙에 작은 텍스트만 배치 ----
  const stageLbl = document.createElementNS(NS, "text");
  stageLbl.setAttribute("x", ((seatMinX + seatMaxX) / 2).toFixed(2));
  stageLbl.setAttribute("y", (seatMinY - rowSlot * 2.2).toFixed(2));
  stageLbl.setAttribute("text-anchor", "middle");
  stageLbl.setAttribute("dominant-baseline", "central");
  stageLbl.setAttribute("fill", "#f5d442");
  stageLbl.setAttribute("font-size", "1.8");
  stageLbl.setAttribute("font-weight", "bold");
  stageLbl.setAttribute("letter-spacing", "0.2");
  stageLbl.setAttribute("pointer-events", "none");
  stageLbl.textContent = "↑ 무대";
  svg.appendChild(stageLbl);

  grid.appendChild(svg);
}

// 섹션 화면에서 뒤로가기
document.getElementById("btn-section-back").addEventListener("click", () => {
  document.querySelectorAll(".mode-screen").forEach(s => s.classList.remove("active"));
  document.getElementById("screen-concert").classList.add("active");
  renderConcertSeats(); // taken 상태 반영
});

// ---------- 좌석 클릭 ----------
function onSeatClick(el, seat) {
  if (state.running || seat.taken) return;
  el.classList.add("selected");
  showResult({
    title: "예약 완료!",
    lines: [
      `좌석: ${el.textContent}`,
      state.mode === "concert" ? `구역: ${seat.section}` : `등급: ${seat.type}`,
      `가격: ${fmt(seat.price)}`,
    ],
  });
}

// ---------- 시뮬레이션 시작 (봇 경쟁) ----------
// 공연장 시작하기: 위 bindConcertStart() 에서 시계 오버레이를 시작한다.
// (여기서 startSim을 호출하지 않음 — 좌석은 보안문자·날짜/시간 이후에 열린다.)
document.getElementById("btn-concert-reset").addEventListener("click", () => resetSim("concert"));

document.getElementById("btn-back-home").addEventListener("click", () => {
  document.getElementById("screen-result").classList.remove("active");
  document.getElementById(`screen-${state.mode}`).classList.add("active");
  // 좌석 맵 → 홈 영역으로 복귀 (처음으로 후 다시 시작할 때 공연명 입력 화면이 보여야 함)
  const homeArea = document.getElementById("concert-home-area");
  const seatArea = document.getElementById("concert-seat-area");
  if (homeArea) homeArea.classList.remove("hidden");
  if (seatArea) seatArea.classList.add("hidden");
  resetSim(state.mode);
});

function startSim(mode) {
  if (mode === "concert") {
    // 공연장: 시작 버튼을 누르면 전면에 초 단위 카운트다운 시계가 등장 (11:59:30 → 12:00:00)
    hidePreSalePopup();
    startTicketClock();
    return;
  }
  if (state.running) return;
  state.running = true;
  state.startTime = Date.now();
  runSeatSim(mode);
}

function updateSeatVisual(seat) {
  const el = document.querySelector(`[data-id="${seat.id}"]`);
  if (el) { el.classList.add("taken"); el.disabled = true; }
}

function resetSim(mode) {
  state.running = false;
  state.seats = [];
  if (mode === "concert") Object.keys(_sectionTaken).forEach(k => delete _sectionTaken[k]);
  // _flow 상태 초기화 — "처음으로" 후 다시 시작할 때 이전 선택값이 남지 않도록
  _flow.step = null;
  _flow.selectedZone = null;
  _flow.selectedSeatInfo = null;
  _flow.selectedDay = null;
  _flow.selectedTime = null;
  _flow.captchaCode = null;
  initScreen(mode);
}

// ---------- 결과 표시 ----------
function showResult({ title, lines, score }) {
  const screen = document.getElementById("screen-result");
  const content = document.getElementById("result-content");
  let html = `<h3>${title}</h3>`;
  if (score != null) {
    html += `<div style="margin:18px auto 6px;text-align:center;">
      <div style="font-size:15px;font-weight:700;color:#b45309;background:#fef3c7;border:2px solid #f59e0b;border-radius:999px;padding:6px 22px;display:inline-block;">🏆 선점 점수</div>
      <div style="font-size:64px;font-weight:900;line-height:1.15;color:#d97706;margin-top:6px;">${score}<span style="font-size:28px;font-weight:700;"> / 100점</span></div>
    </div>`;
  }
  html += `<ul>${lines.map(l => `<li>${l}</li>`).join("")}</ul>`;
  content.innerHTML = html;
  document.querySelectorAll(".mode-screen").forEach(s => s.classList.remove("active"));
  screen.classList.add("active");
}

// ---------- 티켓팅 카운트다운 시계 (11:59:30 → 12:00:00) ----------
const TICKET_OPEN_MS = 30 * 1000; // 시계 시작(11:59:30)부터 오픈(12:00:00)까지 30초
let _clockTimer = null;
let _clockOpenAt = null;   // Date.now() 기준 12:00:00 도달 시각
let _ticketingOpen = false; // true면 예매 가능

function startTicketClock() {
  stopTicketClock();
  const overlay = document.getElementById("clock-overlay");
  const timeEl = document.getElementById("clock-time");
  if (!overlay || !timeEl) return;

  _ticketingOpen = false;
  _clockOpenAt = Date.now() + TICKET_OPEN_MS;
  overlay.classList.remove("hidden", "open");

  const tick = () => {
    const remain = _clockOpenAt - Date.now();
    if (remain <= 0) {
      // 12:00:00 정각 — 티켓팅 시작
      timeEl.textContent = "12:00:00";
      overlay.classList.add("open");
      _ticketingOpen = true;
      clearInterval(_clockTimer);
      _clockTimer = null;
      return;
    }
    // 남은 초만큼 11:59:30 ~ 11:59:59 를 표시 (오픈 시각을 12:00:00으로 고정 계산)
    const totalSec = Math.round((12 * 3600) - remain / 1000); // 12:00:00 기준 경과 (반올림으로 매초 정확히 1초 증가)
    const h = Math.floor(totalSec / 3600) % 24;
    const m = Math.floor((totalSec % 3600) / 60);
    const s = totalSec % 60;
    timeEl.textContent = `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  };
  tick();
  _clockTimer = setInterval(tick, 1000);
}

function stopTicketClock() {
  if (_clockTimer) { clearInterval(_clockTimer); _clockTimer = null; }
  const overlay = document.getElementById("clock-overlay");
  if (overlay) overlay.classList.add("hidden");
}

// ---------- 예매 시간 전 팝업 ----------
function showPreSalePopup() {
  const popup = document.getElementById("presale-popup");
  if (popup) popup.classList.remove("hidden");
}
function hidePreSalePopup() {
  const popup = document.getElementById("presale-popup");
  if (popup) popup.classList.add("hidden");
}
const _popupCloseBtn = document.getElementById("btn-popup-close");
if (_popupCloseBtn) _popupCloseBtn.addEventListener("click", hidePreSalePopup);

let _queueActive = false;      // 예매 버튼 누른 뒤 대기 화면 진행 중
let _queueWaiters = 0;         // 내 앞 대기 인원 (배정 후 감소만)
let _queueOpenAt = null;       // 판매 시작 시각 (Date.now())
let _queueNextTimer = null;    // 다음 갱신 setTimeout id
let _queueInitialWaiters = 0;  // 대기 화면 진입 시점의 대기 인원 (바 기준 100%)
let _queueRatePerMin = 2500;   // 분당 처리량 (매 갱신마다 2,000~3,000 랜덤)

// 판매 시작 후 지난 시간(초) → [최소, 최대] 대기 인원 범위
function queueRangeForElapsed(elapsedSec) {
  if (elapsedSec < 0.1) return [0, 1000];
  if (elapsedSec < 0.2) return [1001, 5000];
  if (elapsedSec < 0.3) return [5001, 12000];
  if (elapsedSec < 0.4) return [12001, 20000];
  if (elapsedSec < 0.5) return [20001, 30000];
  if (elapsedSec < 0.6) return [30001, 40000];
  if (elapsedSec < 0.7) return [40001, 50000];
  if (elapsedSec < 0.8) return [50001, 60000];
  if (elapsedSec < 0.9) return [60001, 70000];
  if (elapsedSec < 1.0) return [70001, 80000];
  if (elapsedSec < 1.5) return [80001, 100000];
  if (elapsedSec < 2.0) return [100001, 120000];
  // 2초 이후: 매초 +50,000 (2~3초 120,001~170,000 / 3~4초 170,001~220,000 …)
  const n = Math.floor(elapsedSec); // 2 이상
  return [120000 + (n - 2) * 50000 + 1, 170000 + (n - 2) * 50000];
}

// 매 갱신마다 분당 처리량을 2,000~3,000명 사이에서 랜덤하게 다시 정함
function rollQueueRate() {
  _queueRatePerMin = randInt(2000, 3000);
}

// 현재 분당 처리량과 갱신 간격(5~10초) 기준, 이번 갱신에 빠질 인원
function queueTickDelta() {
  const avgInterval = 7.5; // 5~10초 랜덤 간격의 평균
  return Math.max(1, Math.round((_queueRatePerMin / 60) * avgInterval));
}

// 현재 대기 인원이 0이 될 때까지 남은 실제 대기 시간(초)을 분당 처리량으로 추정
function queueEtaSeconds() {
  if (_queueWaiters <= 0) return 0;
  return Math.max(1, Math.ceil((_queueWaiters / _queueRatePerMin) * 60));
}
function queueIntervalRange() {
  // 고정 5~10초 랜덤 간격으로 대기 인원 갱신
  return [5, 10];
}

function randInt(min, max) { return min + Math.floor(Math.random() * (max - min + 1)); }

// 예매 버튼(12시 이후) 클릭 시 호출 — 대기 화면 시작
function startQueue() {
  _queueActive = true;
  _queueOpenAt = Date.now(); // 대기 화면 진입 시각

  // "판매 시작" = 12:00:00 정각(_clockOpenAt). 버튼 클릭 시점 기준이 아니라
  // 오픈 이후로 얼마나 지났는지에 따라 구간이 달라져야 한다.
  const openRef = (_clockOpenAt && _clockOpenAt > 0) ? _clockOpenAt : _queueOpenAt;
  const elapsed = Math.max(0, (Date.now() - openRef) / 1000);
  const [lo, hi] = queueRangeForElapsed(elapsed);
  _queueWaiters = randInt(lo, hi);
  _queueInitialWaiters = _queueWaiters; // 진입 시점 대기 인원을 바 기준 100%로 설정
  rollQueueRate(); // 첫 갱신 전에도 ETA 계산에 쓸 분당 처리량 초기화

  renderQueueScreen(true);
  updateQueueBar(); // 진입 시점 대기 인원이 100%이므로 바로 바 표시
  scheduleNextUpdate();
}

function renderQueueScreen(first) {
  const overlay = document.getElementById("queue-overlay");
  if (overlay) overlay.classList.remove("hidden");
  updateQueueDisplay();
}

function updateQueueDisplay() {
  const countEl = document.getElementById("queue-count");
  const etaEl = document.getElementById("queue-eta");
  if (!countEl) return;
  countEl.textContent = _queueWaiters.toLocaleString();
  const etaSec = queueEtaSeconds();
  if (etaSec >= 60) {
    const m = Math.floor(etaSec / 60);
    const s = etaSec % 60;
    etaEl.textContent = `${m}분 ${s}초`;
  } else {
    etaEl.textContent = `${etaSec}초`;
  }
}

// 무작위 간격으로 다음 갱신을 예약 (고정 간격이 아님)
function scheduleNextUpdate() {
  if (!_queueActive) return;
  const [lo, hi] = queueIntervalRange();
  const delaySec = randInt(lo, hi);
  _queueNextTimer = setTimeout(tickQueue, delaySec * 1000);
}

function tickQueue() {
  if (!_queueActive) return;
  rollQueueRate(); // 이번 갱신의 분당 처리량을 2,000~3,000 사이에서 다시 정함
  const delta = Math.min(queueTickDelta(), _queueWaiters);
  _queueWaiters -= delta;
  updateQueueDisplay();
  updateQueueBar();
  if (_queueWaiters <= 0) {
    // 대기 인원이 0명이 된 순간부터 2초 동안 "0명" 표시를 유지한 뒤
    // 보안 문자 입력 단계로 넘어간다. (즉시 stopQueue 하면 박스가 사라진다)
    _queueActive = false;
    if (_queueNextTimer) { clearTimeout(_queueNextTimer); _queueNextTimer = null; }
    sessionStorage.setItem("_ticketQueueDone", "1"); // 새로고침 대비: 0 도달 시점 저장
    setTimeout(() => {
      sessionStorage.removeItem("_ticketQueueDone");
      enterCaptchaStep();
    }, 2000);
    return;
  }
  scheduleNextUpdate();
}

// 상태창 바: 진입 시점 대기 인원을 100%로 두고 남은 대기 인원 비율 표시
function updateQueueBar() {
  const remEl = document.getElementById("seats-remaining");
  const totEl = document.getElementById("seats-total");
  const fill = document.getElementById("seats-bar-fill");
  if (!remEl || !totEl || !fill) return;
  remEl.textContent = _queueWaiters.toLocaleString();
  totEl.textContent = _queueInitialWaiters.toLocaleString();
  const pct = _queueInitialWaiters > 0 ? (_queueWaiters / _queueInitialWaiters) * 100 : 0;
  fill.style.width = pct + "%";
  // 색: 50% 이상 초록, 20~50% 주황, 20% 미만 빨강
  if (pct >= 50) fill.className = "seat-status-fill ok";
  else if (pct >= 20) fill.className = "seat-status-fill warn";
  else fill.className = "seat-status-fill danger";
}

function stopQueue() {
  _queueActive = false;
  if (_queueNextTimer) { clearTimeout(_queueNextTimer); _queueNextTimer = null; }
  const box = document.getElementById("queue-box");
  if (box) box.remove();
}

// 좌석 선택 단계 진입 — 새 흐름에서 '날짜/시간 다음 단계' 호출.
// (이전엔 대기열 종료 시 여기서 바로 열렸는데, 지금은 security flow 사이를 거쳐 온다.)
function enterSeatSelection(zoneId) {
  return enterSeatStep(zoneId || _flow.selectedZone);
}

// ---------- 초기화 ----------
renderConfigValues();
// 공연장은 홈 화면에서 시작하기 버튼을 누를 때만 좌석 맵을 그린다.
// (무대 그림 없이 시작 버튼 + 임시 스위치만 노출)
if (state.mode !== "concert") initScreen(state.mode);

// 시계 오버레이 예매하기 버튼 — 오픈 전에는 팝업, 오픈 후에는 대기열 진입
const btnClockBook = document.getElementById("btn-clock-book");
if (btnClockBook) {
  btnClockBook.addEventListener("click", () => {
    if (!_ticketingOpen) {
      showPreSalePopup();
    } else {
      // 티켓팅 시작 — 대기열 화면으로 진입 (내 앞 대기 인원 배정 + 갱신 시작)
      startQueue();
    }
  });
}

// ---------- 새로고침 복구: 대기열 0 도달 후 F5를 눌렀을 때 보안문자 단계로 복원 ----------
(function restoreAfterRefresh() {
  const flag = sessionStorage.getItem("_ticketQueueDone");
  if (flag === "1") {
    sessionStorage.removeItem("_ticketQueueDone");
    // DOM 준비가 끝난 뒤 전환 (스크립트가 body 하단에 있어 이미 ready 상태)
    enterCaptchaStep();
  }
})();



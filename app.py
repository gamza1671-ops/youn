"""
전국 병원 비급여진료비 조회 앱
건강보험심사평가원(HIRA) 공공데이터 API + Ollama 챗봇
API 키 없을 때는 샘플 데이터로 동작
"""

import os, json, requests, xml.etree.ElementTree as ET
import threading, pickle, time
from functools import wraps
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, request, jsonify, Response, stream_with_context, session, redirect, url_for
import pandas as pd

# .env 자동 로드 (python-dotenv 미설치 환경 대응)
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    for _line in open(_env_path):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "hira-default-secret-key-change-me")
app.config["PERMANENT_SESSION_LIFETIME"] = 86400 * 7  # 7일

SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "hira1234")

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated

API_KEY    = os.environ.get("HIRA_API_KEY", "")
BASE_URL   = "https://apis.data.go.kr/B551182/nonPaymentDamtInfoService"
OPERATION  = "getNonPaymentItemHospDtlList"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "qwen2.5:7b")
MFDS_API_KEY = os.environ.get("MFDS_API_KEY", "")
MFDS_DRUG_URL = "https://apis.data.go.kr/1471000/DrbEasyDrugInfoService/getDrbEasyDrugList"

# ── 전체 데이터 캐시 ─────────────────────────────────────────────
CACHE_FILE   = os.path.join(os.path.dirname(__file__), "cache.pkl")
CACHE_MAX_AGE = 24 * 3600   # 24시간마다 갱신

CACHE_LOCK   = threading.Lock()
CACHE_DATA   = []          # 전체 비급여 레코드
CACHE_STATUS = {
    "state":   "idle",     # idle | loading | ready | error
    "pages_done": 0,
    "pages_total": 0,
    "count":   0,
    "error":   "",
}

# ── 코드 테이블 ──────────────────────────────────────────────────
GBN_CODES = [
    ("",   "전체",     "🏥"),
    ("01", "행위",     "💊"),
    ("03", "치료재료", "🩹"),
    ("99", "기타",     "📋"),
]
# ※ 약제(02)는 HIRA 비급여 공시 대상 아님 → 탭 미표시

GBN_LABEL = {"01":"행위","02":"약제","03":"치료재료","99":"기타"}

CL_CODES = [
    ("",   "전체"),("01","상급종합"),("11","종합병원"),("21","병원"),
    ("28","요양병원"),("29","정신병원"),("31","의원"),("41","치과병원"),
    ("42","치과의원"),("51","조산원"),("61","보건소"),("71","한방병원"),("72","한의원"),
]

SIDO_CODES = [
    ("","전국"),("110000","서울"),("210000","부산"),("230000","대구"),("220000","인천"),
    ("240000","광주"),("250000","대전"),("260000","울산"),("310000","경기"),
    ("320000","강원"),("330000","충북"),("340000","충남"),("350000","전북"),("360000","전남"),
    ("370000","경북"),("380000","경남"),("390000","제주"),
]

# ── 샘플 데이터 (API 키 없을 때 사용) ────────────────────────────
MOCK_DATA = [
  # 행위(01)
  {"yadmNm":"서울대학교병원","clCdNm":"상급종합병원","addr":"서울특별시 종로구 대학로 101","gbnCd":"01","gbnCdNm":"행위","itemNm":"MRI(뇌, 뇌혈관)","curAmt":850000,"minAmt":750000,"maxAmt":1200000},
  {"yadmNm":"삼성서울병원","clCdNm":"상급종합병원","addr":"서울특별시 강남구 일원로 81","gbnCd":"01","gbnCdNm":"행위","itemNm":"MRI(뇌, 뇌혈관)","curAmt":900000,"minAmt":800000,"maxAmt":1300000},
  {"yadmNm":"서울아산병원","clCdNm":"상급종합병원","addr":"서울특별시 송파구 올림픽로43길 88","gbnCd":"01","gbnCdNm":"행위","itemNm":"MRI(뇌, 뇌혈관)","curAmt":870000,"minAmt":770000,"maxAmt":1250000},
  {"yadmNm":"연세세브란스병원","clCdNm":"상급종합병원","addr":"서울특별시 서대문구 연세로 50-1","gbnCd":"01","gbnCdNm":"행위","itemNm":"MRI(뇌, 뇌혈관)","curAmt":840000,"minAmt":760000,"maxAmt":1180000},
  {"yadmNm":"분당서울대학교병원","clCdNm":"상급종합병원","addr":"경기도 성남시 분당구 구미로173번길 82","gbnCd":"01","gbnCdNm":"행위","itemNm":"MRI(뇌, 뇌혈관)","curAmt":830000,"minAmt":740000,"maxAmt":1180000},
  {"yadmNm":"부산대학교병원","clCdNm":"상급종합병원","addr":"부산광역시 서구 구덕로 179","gbnCd":"01","gbnCdNm":"행위","itemNm":"MRI(뇌, 뇌혈관)","curAmt":780000,"minAmt":700000,"maxAmt":1100000},
  {"yadmNm":"서울대학교병원","clCdNm":"상급종합병원","addr":"서울특별시 종로구 대학로 101","gbnCd":"01","gbnCdNm":"행위","itemNm":"MRI(척추)","curAmt":620000,"minAmt":560000,"maxAmt":920000},
  {"yadmNm":"강남세브란스병원","clCdNm":"상급종합병원","addr":"서울특별시 강남구 언주로 211","gbnCd":"01","gbnCdNm":"행위","itemNm":"MRI(척추)","curAmt":600000,"minAmt":550000,"maxAmt":900000},
  {"yadmNm":"아주대학교병원","clCdNm":"상급종합병원","addr":"경기도 수원시 영통구 월드컵로 164","gbnCd":"01","gbnCdNm":"행위","itemNm":"CT(복부)","curAmt":310000,"minAmt":270000,"maxAmt":440000},
  {"yadmNm":"경북대학교병원","clCdNm":"상급종합병원","addr":"대구광역시 중구 달성로 56","gbnCd":"01","gbnCdNm":"행위","itemNm":"CT(복부)","curAmt":300000,"minAmt":260000,"maxAmt":420000},
  {"yadmNm":"한양대학교병원","clCdNm":"상급종합병원","addr":"서울특별시 성동구 왕십리로 222-1","gbnCd":"01","gbnCdNm":"행위","itemNm":"CT(흉부)","curAmt":280000,"minAmt":240000,"maxAmt":390000},
  {"yadmNm":"연세세브란스병원","clCdNm":"상급종합병원","addr":"서울특별시 서대문구 연세로 50-1","gbnCd":"01","gbnCdNm":"행위","itemNm":"CT(흉부)","curAmt":290000,"minAmt":250000,"maxAmt":400000},
  {"yadmNm":"서울대학교병원","clCdNm":"상급종합병원","addr":"서울특별시 종로구 대학로 101","gbnCd":"01","gbnCdNm":"행위","itemNm":"초음파(복부)","curAmt":180000,"minAmt":150000,"maxAmt":250000},
  {"yadmNm":"부산대학교병원","clCdNm":"상급종합병원","addr":"부산광역시 서구 구덕로 179","gbnCd":"01","gbnCdNm":"행위","itemNm":"초음파(복부)","curAmt":160000,"minAmt":140000,"maxAmt":230000},
  {"yadmNm":"인하대학교병원","clCdNm":"상급종합병원","addr":"인천광역시 중구 인하로 27","gbnCd":"01","gbnCdNm":"행위","itemNm":"초음파(복부)","curAmt":155000,"minAmt":130000,"maxAmt":220000},
  {"yadmNm":"인천성모병원","clCdNm":"종합병원","addr":"인천광역시 부평구 동수로 56","gbnCd":"01","gbnCdNm":"행위","itemNm":"초음파(복부)","curAmt":140000,"minAmt":120000,"maxAmt":200000},
  {"yadmNm":"서울아산병원","clCdNm":"상급종합병원","addr":"서울특별시 송파구 올림픽로43길 88","gbnCd":"01","gbnCdNm":"행위","itemNm":"초음파(심장)","curAmt":220000,"minAmt":190000,"maxAmt":300000},
  {"yadmNm":"제주대학교병원","clCdNm":"상급종합병원","addr":"제주특별자치도 제주시 아란13길 15","gbnCd":"01","gbnCdNm":"행위","itemNm":"초음파(심장)","curAmt":200000,"minAmt":170000,"maxAmt":280000},
  {"yadmNm":"삼성서울병원","clCdNm":"상급종합병원","addr":"서울특별시 강남구 일원로 81","gbnCd":"01","gbnCdNm":"행위","itemNm":"도수치료","curAmt":120000,"minAmt":80000,"maxAmt":180000},
  {"yadmNm":"고려대학교안암병원","clCdNm":"상급종합병원","addr":"서울특별시 성북구 인촌로 73","gbnCd":"01","gbnCdNm":"행위","itemNm":"도수치료","curAmt":100000,"minAmt":70000,"maxAmt":160000},
  {"yadmNm":"전남대학교병원","clCdNm":"상급종합병원","addr":"광주광역시 동구 제봉로 42","gbnCd":"01","gbnCdNm":"행위","itemNm":"도수치료","curAmt":90000,"minAmt":60000,"maxAmt":140000},
  {"yadmNm":"강동경희대학교병원","clCdNm":"상급종합병원","addr":"서울특별시 강동구 동남로 892","gbnCd":"01","gbnCdNm":"행위","itemNm":"도수치료","curAmt":110000,"minAmt":75000,"maxAmt":170000},
  {"yadmNm":"경희대학교병원","clCdNm":"상급종합병원","addr":"서울특별시 동대문구 경희대로 26","gbnCd":"01","gbnCdNm":"행위","itemNm":"체외충격파치료","curAmt":80000,"minAmt":50000,"maxAmt":120000},
  {"yadmNm":"충남대학교병원","clCdNm":"상급종합병원","addr":"대전광역시 중구 문화로 282","gbnCd":"01","gbnCdNm":"행위","itemNm":"MRI(척추)","curAmt":570000,"minAmt":520000,"maxAmt":850000},
  {"yadmNm":"원주세브란스기독병원","clCdNm":"상급종합병원","addr":"강원도 원주시 일산로 20","gbnCd":"01","gbnCdNm":"행위","itemNm":"MRI(척추)","curAmt":560000,"minAmt":500000,"maxAmt":820000},
  {"yadmNm":"을지대학교을지병원","clCdNm":"종합병원","addr":"서울특별시 노원구 한글비석로 68","gbnCd":"01","gbnCdNm":"행위","itemNm":"MRI(뇌, 뇌혈관)","curAmt":720000,"minAmt":650000,"maxAmt":1050000},
  {"yadmNm":"서울대학교병원","clCdNm":"상급종합병원","addr":"서울특별시 종로구 대학로 101","gbnCd":"01","gbnCdNm":"행위","itemNm":"수면내시경(위)","curAmt":150000,"minAmt":120000,"maxAmt":200000},
  {"yadmNm":"삼성서울병원","clCdNm":"상급종합병원","addr":"서울특별시 강남구 일원로 81","gbnCd":"01","gbnCdNm":"행위","itemNm":"수면내시경(위)","curAmt":160000,"minAmt":130000,"maxAmt":210000},
  {"yadmNm":"강남나이스의원","clCdNm":"의원","addr":"서울특별시 강남구 테헤란로 152","gbnCd":"01","gbnCdNm":"행위","itemNm":"수면내시경(위)","curAmt":100000,"minAmt":80000,"maxAmt":140000},
  {"yadmNm":"부산미래내과의원","clCdNm":"의원","addr":"부산광역시 해운대구 센텀중앙로 55","gbnCd":"01","gbnCdNm":"행위","itemNm":"수면내시경(위)","curAmt":90000,"minAmt":70000,"maxAmt":130000},
  {"yadmNm":"연세세브란스병원","clCdNm":"상급종합병원","addr":"서울특별시 서대문구 연세로 50-1","gbnCd":"01","gbnCdNm":"행위","itemNm":"백내장수술(단초점렌즈)","curAmt":800000,"minAmt":700000,"maxAmt":1100000},
  {"yadmNm":"김안과병원","clCdNm":"종합병원","addr":"서울특별시 영등포구 영중로 17","gbnCd":"01","gbnCdNm":"행위","itemNm":"백내장수술(단초점렌즈)","curAmt":750000,"minAmt":650000,"maxAmt":1000000},
  {"yadmNm":"누네안과병원","clCdNm":"병원","addr":"서울특별시 강남구 강남대로 536","gbnCd":"01","gbnCdNm":"행위","itemNm":"백내장수술(다초점렌즈)","curAmt":2500000,"minAmt":2000000,"maxAmt":3500000},
  {"yadmNm":"강남BGN안과","clCdNm":"의원","addr":"서울특별시 강남구 역삼로 206","gbnCd":"01","gbnCdNm":"행위","itemNm":"라식수술","curAmt":1200000,"minAmt":900000,"maxAmt":1500000},
  {"yadmNm":"부산성모안과의원","clCdNm":"의원","addr":"부산광역시 부산진구 중앙대로 672","gbnCd":"01","gbnCdNm":"행위","itemNm":"라식수술","curAmt":1100000,"minAmt":850000,"maxAmt":1400000},
  {"yadmNm":"서울대학교치과병원","clCdNm":"치과병원","addr":"서울특별시 종로구 대학로 101","gbnCd":"01","gbnCdNm":"행위","itemNm":"임플란트(1개)","curAmt":1500000,"minAmt":1200000,"maxAmt":2000000},
  {"yadmNm":"연세대학교치과대학병원","clCdNm":"치과병원","addr":"서울특별시 서대문구 연세로 50-1","gbnCd":"01","gbnCdNm":"행위","itemNm":"임플란트(1개)","curAmt":1400000,"minAmt":1100000,"maxAmt":1900000},
  {"yadmNm":"강남임플란트치과의원","clCdNm":"치과의원","addr":"서울특별시 강남구 강남대로 382","gbnCd":"01","gbnCdNm":"행위","itemNm":"임플란트(1개)","curAmt":1200000,"minAmt":900000,"maxAmt":1600000},
  {"yadmNm":"부산미래치과의원","clCdNm":"치과의원","addr":"부산광역시 연제구 연산로 56","gbnCd":"01","gbnCdNm":"행위","itemNm":"임플란트(1개)","curAmt":1100000,"minAmt":850000,"maxAmt":1500000},
  {"yadmNm":"경희대학교병원","clCdNm":"상급종합병원","addr":"서울특별시 동대문구 경희대로 26","gbnCd":"01","gbnCdNm":"행위","itemNm":"레이저치료(피부)","curAmt":150000,"minAmt":100000,"maxAmt":220000},
  {"yadmNm":"강남피부과의원","clCdNm":"의원","addr":"서울특별시 강남구 압구정로 201","gbnCd":"01","gbnCdNm":"행위","itemNm":"레이저치료(피부)","curAmt":120000,"minAmt":80000,"maxAmt":180000},
  {"yadmNm":"서울대학교병원","clCdNm":"상급종합병원","addr":"서울특별시 종로구 대학로 101","gbnCd":"01","gbnCdNm":"행위","itemNm":"독감예방접종","curAmt":30000,"minAmt":25000,"maxAmt":40000},
  {"yadmNm":"강동성심병원","clCdNm":"종합병원","addr":"서울특별시 강동구 성안로 150","gbnCd":"01","gbnCdNm":"행위","itemNm":"독감예방접종","curAmt":28000,"minAmt":22000,"maxAmt":38000},
  {"yadmNm":"경기연세내과의원","clCdNm":"의원","addr":"경기도 수원시 팔달구 중부대로 73","gbnCd":"01","gbnCdNm":"행위","itemNm":"독감예방접종","curAmt":25000,"minAmt":20000,"maxAmt":35000},
  # 약제(02)
  # 독감백신(플루아드 계열)
  {"yadmNm":"서울대학교병원","clCdNm":"상급종합병원","addr":"서울특별시 종로구 대학로 101","gbnCd":"02","gbnCdNm":"약제","itemNm":"플루아드 테트라 프리필드시린지 0.5mL","curAmt":38000,"minAmt":35000,"maxAmt":45000},
  {"yadmNm":"삼성서울병원","clCdNm":"상급종합병원","addr":"서울특별시 강남구 일원로 81","gbnCd":"02","gbnCdNm":"약제","itemNm":"플루아드 테트라 프리필드시린지 0.5mL","curAmt":40000,"minAmt":35000,"maxAmt":48000},
  {"yadmNm":"서울아산병원","clCdNm":"상급종합병원","addr":"서울특별시 송파구 올림픽로43길 88","gbnCd":"02","gbnCdNm":"약제","itemNm":"플루아드 테트라 프리필드시린지 0.5mL","curAmt":39000,"minAmt":35000,"maxAmt":46000},
  {"yadmNm":"연세세브란스병원","clCdNm":"상급종합병원","addr":"서울특별시 서대문구 연세로 50-1","gbnCd":"02","gbnCdNm":"약제","itemNm":"플루아드 테트라 프리필드시린지 0.5mL","curAmt":38000,"minAmt":34000,"maxAmt":45000},
  {"yadmNm":"분당서울대학교병원","clCdNm":"상급종합병원","addr":"경기도 성남시 분당구 구미로173번길 82","gbnCd":"02","gbnCdNm":"약제","itemNm":"플루아드 테트라 프리필드시린지 0.5mL","curAmt":37000,"minAmt":33000,"maxAmt":44000},
  {"yadmNm":"강동성심병원","clCdNm":"종합병원","addr":"서울특별시 강동구 성안로 150","gbnCd":"02","gbnCdNm":"약제","itemNm":"플루아드 테트라 프리필드시린지 0.5mL","curAmt":35000,"minAmt":30000,"maxAmt":42000},
  {"yadmNm":"인하대학교병원","clCdNm":"상급종합병원","addr":"인천광역시 중구 인하로 27","gbnCd":"02","gbnCdNm":"약제","itemNm":"플루아드 테트라 프리필드시린지 0.5mL","curAmt":36000,"minAmt":32000,"maxAmt":43000},
  {"yadmNm":"경희대학교병원","clCdNm":"상급종합병원","addr":"서울특별시 동대문구 경희대로 26","gbnCd":"02","gbnCdNm":"약제","itemNm":"플루아드 테트라 프리필드시린지 0.5mL","curAmt":37000,"minAmt":33000,"maxAmt":44000},
  {"yadmNm":"부산대학교병원","clCdNm":"상급종합병원","addr":"부산광역시 서구 구덕로 179","gbnCd":"02","gbnCdNm":"약제","itemNm":"플루아드 테트라 프리필드시린지 0.5mL","curAmt":36000,"minAmt":32000,"maxAmt":43000},
  {"yadmNm":"경북대학교병원","clCdNm":"상급종합병원","addr":"대구광역시 중구 달성로 56","gbnCd":"02","gbnCdNm":"약제","itemNm":"플루아드 테트라 프리필드시린지 0.5mL","curAmt":35000,"minAmt":31000,"maxAmt":42000},
  {"yadmNm":"전남대학교병원","clCdNm":"상급종합병원","addr":"광주광역시 동구 제봉로 42","gbnCd":"02","gbnCdNm":"약제","itemNm":"플루아드 테트라 프리필드시린지 0.5mL","curAmt":35000,"minAmt":30000,"maxAmt":41000},
  {"yadmNm":"충남대학교병원","clCdNm":"상급종합병원","addr":"대전광역시 중구 문화로 282","gbnCd":"02","gbnCdNm":"약제","itemNm":"플루아드 테트라 프리필드시린지 0.5mL","curAmt":36000,"minAmt":32000,"maxAmt":43000},
  {"yadmNm":"강남내과의원","clCdNm":"의원","addr":"서울특별시 강남구 테헤란로 415","gbnCd":"02","gbnCdNm":"약제","itemNm":"플루아드 테트라 프리필드시린지 0.5mL","curAmt":33000,"minAmt":28000,"maxAmt":40000},
  {"yadmNm":"해운대내과의원","clCdNm":"의원","addr":"부산광역시 해운대구 센텀중앙로 55","gbnCd":"02","gbnCdNm":"약제","itemNm":"플루아드 테트라 프리필드시린지 0.5mL","curAmt":32000,"minAmt":28000,"maxAmt":39000},
  # 기타 약제
  {"yadmNm":"서울대학교병원","clCdNm":"상급종합병원","addr":"서울특별시 종로구 대학로 101","gbnCd":"02","gbnCdNm":"약제","itemNm":"항생제(세파계열)","curAmt":15000,"minAmt":10000,"maxAmt":25000},
  {"yadmNm":"삼성서울병원","clCdNm":"상급종합병원","addr":"서울특별시 강남구 일원로 81","gbnCd":"02","gbnCdNm":"약제","itemNm":"항생제(세파계열)","curAmt":14000,"minAmt":9000,"maxAmt":22000},
  {"yadmNm":"서울아산병원","clCdNm":"상급종합병원","addr":"서울특별시 송파구 올림픽로43길 88","gbnCd":"02","gbnCdNm":"약제","itemNm":"수액(생리식염수 500mL)","curAmt":12000,"minAmt":8000,"maxAmt":18000},
  {"yadmNm":"강남성심병원","clCdNm":"종합병원","addr":"서울특별시 영등포구 신길로 1","gbnCd":"02","gbnCdNm":"약제","itemNm":"수액(생리식염수 500mL)","curAmt":10000,"minAmt":7000,"maxAmt":15000},
  {"yadmNm":"연세세브란스병원","clCdNm":"상급종합병원","addr":"서울특별시 서대문구 연세로 50-1","gbnCd":"02","gbnCdNm":"약제","itemNm":"조영제(CT용 이오헥솔)","curAmt":45000,"minAmt":35000,"maxAmt":65000},
  {"yadmNm":"경북대학교병원","clCdNm":"상급종합병원","addr":"대구광역시 중구 달성로 56","gbnCd":"02","gbnCdNm":"약제","itemNm":"조영제(CT용 이오헥솔)","curAmt":42000,"minAmt":32000,"maxAmt":60000},
  {"yadmNm":"서울대학교병원","clCdNm":"상급종합병원","addr":"서울특별시 종로구 대학로 101","gbnCd":"02","gbnCdNm":"약제","itemNm":"비타민C주사(고용량)","curAmt":35000,"minAmt":25000,"maxAmt":55000},
  {"yadmNm":"강남웰빙내과의원","clCdNm":"의원","addr":"서울특별시 강남구 테헤란로 415","gbnCd":"02","gbnCdNm":"약제","itemNm":"비타민C주사(고용량)","curAmt":30000,"minAmt":20000,"maxAmt":50000},
  {"yadmNm":"서울대학교병원","clCdNm":"상급종합병원","addr":"서울특별시 종로구 대학로 101","gbnCd":"02","gbnCdNm":"약제","itemNm":"독감백신(4가, 일반)","curAmt":28000,"minAmt":22000,"maxAmt":38000},
  {"yadmNm":"강동성심병원","clCdNm":"종합병원","addr":"서울특별시 강동구 성안로 150","gbnCd":"02","gbnCdNm":"약제","itemNm":"독감백신(4가, 일반)","curAmt":25000,"minAmt":20000,"maxAmt":35000},
  {"yadmNm":"경기연세내과의원","clCdNm":"의원","addr":"경기도 수원시 팔달구 중부대로 73","gbnCd":"02","gbnCdNm":"약제","itemNm":"독감백신(4가, 일반)","curAmt":22000,"minAmt":18000,"maxAmt":32000},
  # 삼성창원병원 약제
  {"yadmNm":"삼성창원병원","clCdNm":"종합병원","addr":"경상남도 창원시 마산회원구 팔용로 158","gbnCd":"02","gbnCdNm":"약제","itemNm":"플루아드 테트라 프리필드시린지 0.5mL","curAmt":34000,"minAmt":30000,"maxAmt":40000},
  {"yadmNm":"삼성창원병원","clCdNm":"종합병원","addr":"경상남도 창원시 마산회원구 팔용로 158","gbnCd":"02","gbnCdNm":"약제","itemNm":"독감백신(4가, 일반)","curAmt":24000,"minAmt":20000,"maxAmt":30000},
  {"yadmNm":"삼성창원병원","clCdNm":"종합병원","addr":"경상남도 창원시 마산회원구 팔용로 158","gbnCd":"02","gbnCdNm":"약제","itemNm":"조영제(CT용 이오헥솔)","curAmt":40000,"minAmt":32000,"maxAmt":55000},
  {"yadmNm":"삼성창원병원","clCdNm":"종합병원","addr":"경상남도 창원시 마산회원구 팔용로 158","gbnCd":"02","gbnCdNm":"약제","itemNm":"비타민C주사(고용량)","curAmt":28000,"minAmt":22000,"maxAmt":40000},
  {"yadmNm":"삼성창원병원","clCdNm":"종합병원","addr":"경상남도 창원시 마산회원구 팔용로 158","gbnCd":"01","gbnCdNm":"행위","itemNm":"MRI(뇌, 뇌혈관)","curAmt":760000,"minAmt":680000,"maxAmt":1050000},
  {"yadmNm":"삼성창원병원","clCdNm":"종합병원","addr":"경상남도 창원시 마산회원구 팔용로 158","gbnCd":"01","gbnCdNm":"행위","itemNm":"MRI(척추)","curAmt":550000,"minAmt":490000,"maxAmt":800000},
  {"yadmNm":"삼성창원병원","clCdNm":"종합병원","addr":"경상남도 창원시 마산회원구 팔용로 158","gbnCd":"01","gbnCdNm":"행위","itemNm":"CT(복부)","curAmt":290000,"minAmt":250000,"maxAmt":400000},
  {"yadmNm":"삼성창원병원","clCdNm":"종합병원","addr":"경상남도 창원시 마산회원구 팔용로 158","gbnCd":"01","gbnCdNm":"행위","itemNm":"초음파(복부)","curAmt":145000,"minAmt":120000,"maxAmt":200000},
  {"yadmNm":"삼성창원병원","clCdNm":"종합병원","addr":"경상남도 창원시 마산회원구 팔용로 158","gbnCd":"01","gbnCdNm":"행위","itemNm":"도수치료","curAmt":95000,"minAmt":65000,"maxAmt":145000},
  {"yadmNm":"삼성창원병원","clCdNm":"종합병원","addr":"경상남도 창원시 마산회원구 팔용로 158","gbnCd":"01","gbnCdNm":"행위","itemNm":"수면내시경(위)","curAmt":95000,"minAmt":75000,"maxAmt":130000},
  {"yadmNm":"삼성창원병원","clCdNm":"종합병원","addr":"경상남도 창원시 마산회원구 팔용로 158","gbnCd":"03","gbnCdNm":"치료재료","itemNm":"인공관절(무릎)","curAmt":3400000,"minAmt":2900000,"maxAmt":4800000},
  {"yadmNm":"삼성창원병원","clCdNm":"종합병원","addr":"경상남도 창원시 마산회원구 팔용로 158","gbnCd":"03","gbnCdNm":"치료재료","itemNm":"봉합사(비흡수성)","curAmt":20000,"minAmt":14000,"maxAmt":32000},
  {"yadmNm":"삼성창원병원","clCdNm":"종합병원","addr":"경상남도 창원시 마산회원구 팔용로 158","gbnCd":"99","gbnCdNm":"기타","itemNm":"상급병실료(1인실)","curAmt":200000,"minAmt":170000,"maxAmt":280000},
  {"yadmNm":"삼성창원병원","clCdNm":"종합병원","addr":"경상남도 창원시 마산회원구 팔용로 158","gbnCd":"99","gbnCdNm":"기타","itemNm":"제증명수수료(진단서)","curAmt":20000,"minAmt":15000,"maxAmt":30000},
  # 치료재료(03)
  {"yadmNm":"서울대학교병원","clCdNm":"상급종합병원","addr":"서울특별시 종로구 대학로 101","gbnCd":"03","gbnCdNm":"치료재료","itemNm":"인공관절(무릎)","curAmt":3500000,"minAmt":3000000,"maxAmt":5000000},
  {"yadmNm":"삼성서울병원","clCdNm":"상급종합병원","addr":"서울특별시 강남구 일원로 81","gbnCd":"03","gbnCdNm":"치료재료","itemNm":"인공관절(무릎)","curAmt":3800000,"minAmt":3200000,"maxAmt":5500000},
  {"yadmNm":"연세세브란스병원","clCdNm":"상급종합병원","addr":"서울특별시 서대문구 연세로 50-1","gbnCd":"03","gbnCdNm":"치료재료","itemNm":"척추고정나사","curAmt":1200000,"minAmt":900000,"maxAmt":1800000},
  {"yadmNm":"아주대학교병원","clCdNm":"상급종합병원","addr":"경기도 수원시 영통구 월드컵로 164","gbnCd":"03","gbnCdNm":"치료재료","itemNm":"척추고정나사","curAmt":1100000,"minAmt":850000,"maxAmt":1700000},
  {"yadmNm":"서울아산병원","clCdNm":"상급종합병원","addr":"서울특별시 송파구 올림픽로43길 88","gbnCd":"03","gbnCdNm":"치료재료","itemNm":"봉합사(비흡수성)","curAmt":25000,"minAmt":18000,"maxAmt":40000},
  {"yadmNm":"강동성심병원","clCdNm":"종합병원","addr":"서울특별시 강동구 성안로 150","gbnCd":"03","gbnCdNm":"치료재료","itemNm":"봉합사(비흡수성)","curAmt":22000,"minAmt":15000,"maxAmt":35000},
  {"yadmNm":"서울대학교치과병원","clCdNm":"치과병원","addr":"서울특별시 종로구 대학로 101","gbnCd":"03","gbnCdNm":"치료재료","itemNm":"임플란트 픽스쳐","curAmt":800000,"minAmt":600000,"maxAmt":1200000},
  {"yadmNm":"연세대학교치과대학병원","clCdNm":"치과병원","addr":"서울특별시 서대문구 연세로 50-1","gbnCd":"03","gbnCdNm":"치료재료","itemNm":"임플란트 픽스쳐","curAmt":780000,"minAmt":580000,"maxAmt":1150000},
  {"yadmNm":"삼성서울병원","clCdNm":"상급종합병원","addr":"서울특별시 강남구 일원로 81","gbnCd":"03","gbnCdNm":"치료재료","itemNm":"인공수정체(단초점)","curAmt":350000,"minAmt":280000,"maxAmt":500000},
  {"yadmNm":"김안과병원","clCdNm":"종합병원","addr":"서울특별시 영등포구 영중로 17","gbnCd":"03","gbnCdNm":"치료재료","itemNm":"인공수정체(단초점)","curAmt":320000,"minAmt":250000,"maxAmt":480000},
  {"yadmNm":"누네안과병원","clCdNm":"병원","addr":"서울특별시 강남구 강남대로 536","gbnCd":"03","gbnCdNm":"치료재료","itemNm":"인공수정체(다초점)","curAmt":1800000,"minAmt":1500000,"maxAmt":2500000},
  # 기타(99)
  {"yadmNm":"서울대학교병원","clCdNm":"상급종합병원","addr":"서울특별시 종로구 대학로 101","gbnCd":"99","gbnCdNm":"기타","itemNm":"제증명수수료(진단서)","curAmt":20000,"minAmt":15000,"maxAmt":30000},
  {"yadmNm":"삼성서울병원","clCdNm":"상급종합병원","addr":"서울특별시 강남구 일원로 81","gbnCd":"99","gbnCdNm":"기타","itemNm":"제증명수수료(진단서)","curAmt":20000,"minAmt":15000,"maxAmt":30000},
  {"yadmNm":"연세세브란스병원","clCdNm":"상급종합병원","addr":"서울특별시 서대문구 연세로 50-1","gbnCd":"99","gbnCdNm":"기타","itemNm":"제증명수수료(진단서)","curAmt":20000,"minAmt":15000,"maxAmt":30000},
  {"yadmNm":"서울아산병원","clCdNm":"상급종합병원","addr":"서울특별시 송파구 올림픽로43길 88","gbnCd":"99","gbnCdNm":"기타","itemNm":"상급병실료(1인실)","curAmt":300000,"minAmt":250000,"maxAmt":450000},
  {"yadmNm":"삼성서울병원","clCdNm":"상급종합병원","addr":"서울특별시 강남구 일원로 81","gbnCd":"99","gbnCdNm":"기타","itemNm":"상급병실료(1인실)","curAmt":350000,"minAmt":280000,"maxAmt":500000},
  {"yadmNm":"분당서울대학교병원","clCdNm":"상급종합병원","addr":"경기도 성남시 분당구 구미로173번길 82","gbnCd":"99","gbnCdNm":"기타","itemNm":"상급병실료(1인실)","curAmt":280000,"minAmt":230000,"maxAmt":420000},
  {"yadmNm":"서울대학교병원","clCdNm":"상급종합병원","addr":"서울특별시 종로구 대학로 101","gbnCd":"99","gbnCdNm":"기타","itemNm":"상급병실료(2인실)","curAmt":180000,"minAmt":150000,"maxAmt":260000},
  {"yadmNm":"부산대학교병원","clCdNm":"상급종합병원","addr":"부산광역시 서구 구덕로 179","gbnCd":"99","gbnCdNm":"기타","itemNm":"상급병실료(2인실)","curAmt":160000,"minAmt":130000,"maxAmt":240000},
]

MOCK_ITEM_NAMES = sorted({d["itemNm"] for d in MOCK_DATA})


def infer_gbn_cd(npay_kor_nm: str) -> str:
    """npayKorNm 첫 카테고리로 gbnCd 추론 (HIRA 실제 분류 기준)
    - 03 치료재료: "치료재료" prefix
    - 02 약제:     "약제" prefix  (현재 HIRA 데이터에 없음)
    - 99 기타:     상급병실료, 제증명수수료, 선택진료비/료 명시 매칭
    - 01 행위:     나머지 모두 (검사, 처치, 수술, MRI, 초음파 등)
    ※ "처치 및 수술료(기타)" 등 "(기타)" 포함 행위 항목은 01로 분류
    """
    cat = npay_kor_nm.split("/")[0].strip()
    if "치료재료" in cat:
        return "03"
    if "약제" in cat:
        return "02"
    # 비급여 기타 항목 — 명시적 카테고리명으로만 판단 (부분일치 오분류 방지)
    if cat.startswith("상급병실료") or cat.startswith("제증명수수료") \
            or cat.startswith("선택진료비") or cat.startswith("선택진료료"):
        return "99"
    return "01"


def parse_xml_items(xml_text: str):
    """XML 응답을 파싱해 items 리스트와 totalCount 반환"""
    root = ET.fromstring(xml_text)
    total = int(root.findtext(".//totalCount") or "0")
    items = []
    for item in root.findall(".//item"):
        def t(tag): return item.findtext(tag) or ""
        sido  = t("sidoCdNm")
        sggu  = t("sgguCdNm")
        addr  = f"{sido} {sggu}".strip()
        npay_nm = t("npayKorNm")
        items.append({
            "yadmNm":     t("yadmNm"),
            "clCdNm":     t("clCdNm"),
            "addr":       addr,
            "gbnCd":      infer_gbn_cd(npay_nm),
            "gbnCdNm":    GBN_LABEL.get(infer_gbn_cd(npay_nm), "기타"),
            "itemNm":     npay_nm,
            "itemNmHosp": t("yadmNpayCdNm"),
            "npayCd":     t("npayCd"),
            "curAmt":     int(t("curAmt") or "0"),
            "minAmt":     0,
            "maxAmt":     0,
            "urlAddr":    t("urlAddr"),
        })
    return items, total


# ── 전체 캐시 로딩 ───────────────────────────────────────────────
def _fetch_page(page_no, batch_size=1000):
    """단일 페이지 fetch (스레드용)"""
    params = {
        "serviceKey": API_KEY,
        "pageNo":     page_no,
        "numOfRows":  batch_size,
    }
    r = requests.get(f"{BASE_URL}/{OPERATION}", params=params, timeout=60)
    r.raise_for_status()
    items, total = parse_xml_items(r.text)
    return items, total


def load_cache_file():
    """저장된 캐시 파일이 있고 24시간 이내면 로드. 성공 시 True 반환."""
    global CACHE_DATA, CACHE_STATUS
    if not os.path.exists(CACHE_FILE):
        return False
    age = time.time() - os.path.getmtime(CACHE_FILE)
    if age > CACHE_MAX_AGE:
        print(f"[Cache] 파일 만료 ({age/3600:.1f}시간) → 새로 로딩")
        return False
    try:
        print(f"[Cache] 파일에서 로드 중... ({CACHE_FILE})")
        with open(CACHE_FILE, "rb") as f:
            data = pickle.load(f)
        # gbnCd 재계산 (분류 로직 업데이트 반영)
        for it in data:
            gbn = infer_gbn_cd(it.get("itemNm", ""))
            it["gbnCd"]    = gbn
            it["gbnCdNm"]  = GBN_LABEL.get(gbn, "기타")
            it.setdefault("npayCd", "")  # 구버전 캐시 호환
        with CACHE_LOCK:
            CACHE_DATA.extend(data)
            CACHE_STATUS["state"]      = "ready"
            CACHE_STATUS["count"]      = len(CACHE_DATA)
            CACHE_STATUS["pages_done"] = 1
            CACHE_STATUS["pages_total"]= 1
        print(f"[Cache] ✅ 파일 로드 완료! {len(CACHE_DATA):,}건 (캐시 나이: {age/60:.0f}분)")
        return True
    except Exception as e:
        print(f"[Cache] 파일 로드 실패: {e} → API에서 새로 로딩")
        return False


def build_cache():
    """백그라운드에서 전체 데이터를 병렬 fetch해 CACHE_DATA에 저장"""
    global CACHE_DATA, CACHE_STATUS
    if not API_KEY:
        return

    # 캐시 파일이 유효하면 즉시 로드
    if load_cache_file():
        return

    BATCH      = 1000
    MAX_WORKERS = 8

    try:
        # 1) 총 페이지 수 파악
        CACHE_STATUS["state"] = "loading"
        first_items, total_count = _fetch_page(1, BATCH)
        total_pages = -(-total_count // BATCH)   # ceil
        CACHE_STATUS["pages_total"] = total_pages
        print(f"[Cache] 총 {total_count:,}건 / {total_pages}페이지 로딩 시작 (병렬 {MAX_WORKERS})")

        # 첫 페이지 즉시 반영
        with CACHE_LOCK:
            CACHE_DATA.extend(first_items)
            CACHE_STATUS["pages_done"] = 1
            CACHE_STATUS["count"] = len(CACHE_DATA)

        # 2) 나머지 페이지 병렬 fetch
        remaining = list(range(2, total_pages + 1))
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {ex.submit(_fetch_page, p, BATCH): p for p in remaining}
            for fut in as_completed(futures):
                try:
                    items, _ = fut.result()
                    with CACHE_LOCK:
                        CACHE_DATA.extend(items)
                        CACHE_STATUS["pages_done"] += 1
                        CACHE_STATUS["count"] = len(CACHE_DATA)
                    if CACHE_STATUS["pages_done"] % 10 == 0:
                        pct = CACHE_STATUS["pages_done"] / total_pages * 100
                        print(f"[Cache] {CACHE_STATUS['pages_done']}/{total_pages} ({pct:.0f}%) — {CACHE_STATUS['count']:,}건")
                except Exception as e:
                    print(f"[Cache] 페이지 오류: {e}")

        with CACHE_LOCK:
            CACHE_STATUS["state"] = "ready"
            CACHE_STATUS["count"] = len(CACHE_DATA)
        print(f"[Cache] ✅ 완료! 총 {len(CACHE_DATA):,}건 메모리 캐시")

        # 파일로 저장 (다음 재시작 시 즉시 로드)
        try:
            with CACHE_LOCK:
                snapshot = list(CACHE_DATA)
            with open(CACHE_FILE, "wb") as f:
                pickle.dump(snapshot, f, protocol=pickle.HIGHEST_PROTOCOL)
            size_mb = os.path.getsize(CACHE_FILE) / 1024 / 1024
            print(f"[Cache] 💾 파일 저장 완료: {CACHE_FILE} ({size_mb:.1f} MB)")
        except Exception as save_err:
            print(f"[Cache] 파일 저장 실패 (무시): {save_err}")

    except Exception as e:
        CACHE_STATUS["state"] = "error"
        CACHE_STATUS["error"] = str(e)
        print(f"[Cache] ❌ 오류: {e}")


def search_cache(item_nm, sido_cd, cl_cd, yadm_nm, gbn_cd, npay_cd_q, page, num_rows):
    """메모리 캐시에서 포함검색 + 우선순위 정렬
    ※ 경남 종합병원·상급종합병원은 다른 지역 필터와 무관하게 항상 포함
    """
    GYEONGNAM_LABEL  = "경남"
    REQUIRED_CL      = {"종합병원", "상급종합"}   # 종합병원 이상 (API 데이터: 상급종합병원→"상급종합")

    with CACHE_LOCK:
        data = list(CACHE_DATA)

    def is_gyeongnam_major(it):
        return GYEONGNAM_LABEL in it["addr"] and it["clCdNm"] in REQUIRED_CL

    def keyword_match(it, tokens):
        # 스페이스 분리된 모든 토큰이 itemNm 또는 itemNmHosp에 포함되면 매칭
        nm = it["itemNm"].lower()
        nm_h = it["itemNmHosp"].lower()
        return all(t in nm or t in nm_h for t in tokens)

    def sort_key(it, tokens):
        nm = it["itemNm"].lower()
        # 첫 번째 토큰 위치 기준 정렬 (앞에 나올수록 우선)
        pos = nm.find(tokens[0])
        if pos >= 0:
            return pos
        pos_h = it["itemNmHosp"].lower().find(tokens[0])
        return 10000 + pos_h if pos_h >= 0 else 99999

    MAJOR_CL = {"종합병원", "상급종합"}   # 기본 검색 대상 (병원명 미입력 시)

    # 검색어를 스페이스로 분리한 토큰 목록 (AND 검색)
    tokens = item_nm.lower().split() if item_nm else []

    # ── 일반 필터 적용 ──────────────────────────────────────────
    filtered = data
    if npay_cd_q:
        q = npay_cd_q.upper()
        filtered = [it for it in filtered if q in it.get("npayCd", "").upper()]
    if tokens:
        filtered = [it for it in filtered if keyword_match(it, tokens)]
    if yadm_nm:
        kw = yadm_nm.lower()
        filtered = [it for it in filtered if kw in it["yadmNm"].lower()]
    if gbn_cd:
        filtered = [it for it in filtered if it["gbnCd"] == gbn_cd]
    if cl_cd:
        cl_label = dict(CL_CODES).get(cl_cd, "")
        filtered = [it for it in filtered if it["clCdNm"] == cl_label]
    elif not yadm_nm and not npay_cd_q:
        # 병원명·EDI코드 미입력 & 종별 미선택 → 종합병원 이상만 표시 (데이터 과다 방지)
        filtered = [it for it in filtered if it["clCdNm"] in MAJOR_CL]
    if sido_cd:
        sido_label = dict(SIDO_CODES).get(sido_cd, "")
        filtered = [it for it in filtered if sido_label in it["addr"]]

    # ── 경남 종합병원이상 항상 추가 (지역 필터로 제외됐을 경우 보충) ──
    if sido_cd and dict(SIDO_CODES).get(sido_cd, "") != GYEONGNAM_LABEL:
        existing_keys = {(it["yadmNm"], it["itemNm"]) for it in filtered}
        extra = [it for it in data
                 if is_gyeongnam_major(it)
                 and (not tokens    or keyword_match(it, tokens))
                 and (not npay_cd_q or npay_cd_q.upper() in it.get("npayCd", "").upper())
                 and (not gbn_cd    or it["gbnCd"] == gbn_cd)
                 and (not yadm_nm   or yadm_nm.lower() in it["yadmNm"].lower())
                 and (it["yadmNm"], it["itemNm"]) not in existing_keys]
        extra = [dict(it, pinned=True) for it in extra]
        filtered = filtered + extra

    # ── 정렬: 검색어 위치 기준 ──
    if tokens:
        filtered.sort(key=lambda it: sort_key(it, tokens))

    total = len(filtered)
    start = (page - 1) * num_rows
    return filtered[start: start + num_rows], total


def search_mock(item_nm, sido_cd, cl_cd, yadm_nm, gbn_cd, page, num_rows):
    """샘플 데이터 검색 (포함검색)"""
    results = MOCK_DATA[:]
    if item_nm:
        kw = item_nm.lower()
        results = [r for r in results if kw in r["itemNm"].lower()]
    if yadm_nm:
        kw = yadm_nm.lower()
        results = [r for r in results if kw in r["yadmNm"].lower()]
    if gbn_cd:
        results = [r for r in results if r["gbnCd"] == gbn_cd]
    if cl_cd:
        cl_label = dict(CL_CODES).get(cl_cd, "")
        results = [r for r in results if r["clCdNm"] == cl_label]
    if sido_cd:
        sido_label = dict(SIDO_CODES).get(sido_cd, "")
        results = [r for r in results if sido_label in r["addr"]]
    total = len(results)
    start = (page - 1) * num_rows
    return results[start:start + num_rows], total


# ── 수가 데이터 ─────────────────────────────────────────────────
SUGA_DATA = []   # [{구분, 수가코드, EDICODE, 명칭, 보험단가, ...}, ...]

SUGA_SYSTEM_PROMPT = """당신은 삼성창원병원 보험심사팀의 AI 챗봇 "보험똑똑e"입니다.

## 역할
1. **수가 조회**: 원내 수가코드(26.9 기준), 보험단가·산재단가·자보단가·일반단가 제공
2. **보험심사기준**: 수술/처치/치료재료에 대한 심평원 급여·비급여 심사기준 안내
3. **약제 정보**: KIMS 약제정보 및 심평원 보험인정기준 안내

## 수가 조회 답변
- 검색 결과가 제공되면 반드시 그 데이터를 기반으로 답변합니다
- 수가코드·명칭·보험단가·산재단가·자보단가·일반단가를 표 형식으로 정리합니다
- 동일 명칭이 여러 규격/용량으로 존재하면 항목별로 나열합니다
- 검색 결과가 없으면 "해당 명칭의 수가코드를 찾을 수 없습니다. 다른 키워드로 검색해 보세요."

## 보험기준 답변
- 심평원 고시 기준으로 인정상병·투여대상·투여기간·병용제한 항목별 정리
- "심평원 심사기준 종합서비스(biz.hira.or.kr)"에서 최신 기준 확인 안내

## 약제 답변
- 식약처 의약품 정보가 [식약처 의약품 정보] 섹션에 제공된 경우 반드시 해당 데이터를 먼저 기반으로 답변합니다
- 효능효과·용법용량·주의사항을 항목별로 정리합니다
- 심평원 보험인정기준은 biz.hira.or.kr 에서 확인하도록 안내합니다
- 식약처 데이터가 없으면 KIMS(www.kimsonline.co.kr) 확인을 안내합니다

## 규칙
- 근거 없는 내용은 지어내지 않습니다
- 개인정보(환자명·등록번호·진단명)는 요청하거나 언급하지 않습니다
- 항상 한국어로 답변합니다
- 답변은 핵심 정보를 포함하되 간결하게 합니다"""


def load_suga_data():
    global SUGA_DATA
    data_dir = os.path.join(os.path.dirname(__file__), "data")
    specs = [
        ("약제",     "약제(26.9).xlsx",     "약제"),
        ("치료재료", "치료재료(26.9).xlsx", "치재"),
        ("행위",     "행위(26.9).xlsx",     "행위"),
    ]
    all_data = []
    for gbn, fname, sheet in specs:
        path = os.path.join(data_dir, fname)
        if not os.path.exists(path):
            print(f"[Suga] 파일 없음: {path}")
            continue
        try:
            df = pd.read_excel(path, sheet_name=sheet, dtype=str)
            col_map = {}
            for c in df.columns:
                if "대분류명" in c:
                    col_map[c] = "대분류명칭"
                elif "세부분류" in c:
                    col_map[c] = "세부분류명칭"
                else:
                    col_map[c] = c
            df = df.rename(columns=col_map)
            # 문자열 컬럼 NaN → 빈 문자열 (float + str 오류 방지)
            str_cols = ["수가코드", "EDICODE", "명칭", "대분류명칭", "세부분류명칭"]
            for col in str_cols:
                if col in df.columns:
                    df[col] = df[col].fillna("").astype(str)
            for col in ["보험단가", "보호단가", "산재단가", "자보단가", "일반단가"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
            df["구분"] = gbn
            all_data.extend(df.to_dict("records"))
            print(f"[Suga] ✅ {gbn}: {len(df):,}건")
        except Exception as e:
            print(f"[Suga] {gbn} 로드 실패: {e}")
    SUGA_DATA = all_data
    print(f"[Suga] 총 {len(SUGA_DATA):,}건 로드 완료")


_KO_STOP = {
    # 수가 관련 일반명사
    "수가", "코드", "기준", "단가", "가격", "금액", "수가코드", "edi코드",
    "보험단가", "산재단가", "자보단가", "일반단가", "보호단가",
    # 동사·구어체
    "조회", "확인", "검색", "알려", "줘요", "해줘", "해요", "있어", "없어",
    "알려줘", "알려주세요", "보여줘", "보여주세요", "찾아줘", "찾아주세요",
    "말해줘", "설명해줘", "알아봐줘", "조사해줘", "알고", "싶어", "싶어요",
    "궁금해", "궁금한데", "있나요", "없나요", "인가요", "해줘요", "해주세요",
    "뭔지", "뭐가", "싶은데", "싶은지", "뭐야", "이야",
    # 보험 관련 일반명사
    "보험", "급여", "비급여", "청구", "심사", "인정", "정보", "내용",
    "관련", "대해", "대한", "관해",
    # 수량·질문
    "얼마", "얼마야", "얼마예요", "어떻게", "어떤", "몇", "무엇",
}


def _is_korean(s: str) -> bool:
    return any("가" <= c <= "힣" for c in s)


def search_suga(keyword: str, gbn: str = None, limit: int = 30) -> list:
    """명칭 포함 검색 + 코드 전방 일치.
    - 한글 불용어·2자 이하 한글 토큰 제거로 오매칭 방지
    - 수가코드/EDICODE는 코드형 쿼리 또는 단일 영문 토큰일 때만 검색
    """
    import re
    if not keyword or not SUGA_DATA:
        return []

    kw_orig = keyword.strip()
    kw = kw_orig.lower()

    # 코드처럼 생긴 쿼리 (영문+숫자, 4자 이상)
    looks_like_code = bool(re.match(r"^[A-Za-z0-9]{4,}$", kw_orig))

    raw_tokens = kw.split()
    tokens = []
    for t in raw_tokens:
        if t in _KO_STOP:
            continue
        if _is_korean(t) and len(t) < 3:   # 2자 이하 한글(수가·코드 등) 제거
            continue
        if not _is_korean(t) and len(t) < 2:
            continue
        tokens.append(t)

    if not tokens:
        tokens = [kw]

    # 코드 검색: 4자 이상 영문+숫자 조합(수가코드형)일 때만 허용
    allow_code_search = looks_like_code

    seen, results = set(), []
    for item in SUGA_DATA:
        if gbn and item.get("구분") != gbn:
            continue
        nm  = str(item.get("명칭") or "").lower()
        key = str(item.get("수가코드") or "") + str(item.get("구분") or "")
        if key in seen:
            continue

        name_match = any(t in nm for t in tokens)

        code_match = False
        if allow_code_search:
            cd  = str(item.get("수가코드") or "").lower()
            edi = str(item.get("EDICODE") or "").lower()
            code_match = any(
                cd == t or cd.startswith(t) or edi == t or edi.startswith(t)
                for t in tokens
            )

        if name_match or code_match:
            seen.add(key)
            # 정렬 기준: 명칭 내 토큰 등장 위치 (앞일수록 더 관련성 높음)
            pos = min((nm.find(t) for t in tokens if t in nm), default=9999)
            results.append((pos, item))

    # 위치 기준 오름차순 정렬 후 limit 적용
    results.sort(key=lambda x: x[0])
    return [x[1] for x in results[:limit]]


def search_mfds_drug(keyword: str, limit: int = 5) -> list:
    """식약처 e-약은요 API로 의약품 효능·용법·주의사항 조회.
    keyword를 순차적으로 축약하며 결과가 나올 때까지 시도.
    """
    if not MFDS_API_KEY or not keyword:
        return []
    import urllib.parse, re

    # 검색 후보: 원본 → 첫 한글 단어 → 첫 2글자 한글
    candidates = [keyword.strip()]
    m = re.match(r'^([가-힣]+)', keyword.strip())
    if m:
        base = m.group(1)
        if base not in candidates:
            candidates.append(base)
        if len(base) > 4:
            candidates.append(base[:4])

    for q in candidates:
        if len(q) < 2:
            continue
        params = urllib.parse.urlencode({
            "serviceKey": MFDS_API_KEY,
            "itemName": q,
            "pageNo": "1",
            "numOfRows": str(limit),
            "type": "json",
        })
        try:
            resp = requests.get(f"{MFDS_DRUG_URL}?{params}", timeout=8)
            data = resp.json()
            items = data.get("body", {}).get("items", [])
            if not isinstance(items, list):
                items = [items] if items else []
            if not items:
                continue
            return [{
                "약품명":   it.get("itemName", ""),
                "효능효과": it.get("efcyQesitm", ""),
                "용법용량": it.get("useMethodQesitm", ""),
                "주의사항": it.get("atpnQesitm", ""),
            } for it in items]
        except Exception:
            continue
    return []


SYSTEM_PROMPT = """당신은 전국 병원 비급여진료비 조회 서비스의 전문 안내 챗봇입니다.

답변 기준:
- 모든 의료·건강보험 관련 답변은 **보건복지부 고시** 및 **건강보험심사평가원(심평원) 심사기준 종합서비스** 기준을 따릅니다
- 급여·비급여 구분, 청구 기준, 심사 기준, 산정 특례 등은 심평원 고시 및 공고 기준으로 설명합니다
- 기준이 명확하지 않거나 최신 고시를 확인해야 할 경우 "심평원 심사기준 종합서비스(https://biz.hira.or.kr) 또는 보건복지부 고시를 확인하세요"라고 안내합니다

약제 답변 기준:
- 약제의 **효능·효과, 용법·용량, 주의사항, 상호작용** 등은 **KIMS(한국의약정보원, www.kimsonline.co.kr)** 자료를 기준으로 답변합니다
- 약제의 **보험인정기준**은 KIMS 내 [보험인정기준] 항목에 수록된 **최신 고시**를 기준으로 답변합니다
  (고시 번호·시행일 등 출처를 함께 명시하고, 인정기준 내용을 구체적으로 설명합니다)
- 급여 인정기준 답변 시 "급여 인정 상병", "투여 대상", "투여 기간", "병용 제한" 등 항목별로 정리하여 설명합니다
- KIMS 보험인정기준에서 확인이 필요한 경우 "KIMS(www.kimsonline.co.kr) → 해당 약제명 검색 → [보험인정기준] 탭에서 최신 고시를 확인하세요"라고 안내합니다
- 약제 보험인정기준은 고시 개정에 따라 수시로 변경될 수 있으므로 반드시 최신 KIMS 자료를 확인하도록 안내합니다

역할:
- 비급여 항목명 검색을 도와줍니다
- 비급여 진료비, 건강보험 급여 기준, 의료비 관련 궁금증을 심평원·복지부 기준으로 답변합니다
- 약제 효능·인정기준 문의는 KIMS 자료 기준으로 답변합니다
- 검색 명령어 형식: **[SEARCH:키워드]** 를 답변에 포함하면 자동으로 검색이 실행됩니다

거절 규칙:
- 보건복지부 고시·심평원 심사기준·KIMS에 없는 내용은 지어내지 않고 "고시 기준에서 확인되지 않습니다"라고 답합니다
- 특정 병원의 정확한 금액을 단정하지 않습니다. 필요하면 **[SEARCH:키워드]** 로 직접 검색을 안내합니다
- 환자 이름·등록번호·진단명 등 개인정보는 절대 요청하거나 언급하지 않습니다

주의:
- 실제 의료 진단이나 처방은 하지 않습니다
- 급여 기준·비급여 항목·약제 허가사항은 개정에 따라 달라질 수 있음을 안내합니다
- 항상 한국어로 답변합니다
- 답변은 간결하게 3-5문장 이내로 하되, 관련 고시·기준·KIMS 근거를 함께 제시합니다"""


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if session.get("logged_in"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        if request.form.get("password") == SITE_PASSWORD:
            session.permanent = True
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "비밀번호가 올바르지 않습니다"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    global SITE_PASSWORD
    msg = None
    error = None
    if request.method == "POST":
        current = request.form.get("current", "")
        new_pw  = request.form.get("new_pw",  "").strip()
        confirm = request.form.get("confirm",  "").strip()
        if current != SITE_PASSWORD:
            error = "현재 비밀번호가 올바르지 않습니다"
        elif len(new_pw) < 4:
            error = "새 비밀번호는 4자 이상이어야 합니다"
        elif new_pw != confirm:
            error = "새 비밀번호와 확인이 일치하지 않습니다"
        else:
            # .env 파일 업데이트
            env_path = os.path.join(os.path.dirname(__file__), ".env")
            lines = open(env_path).readlines() if os.path.exists(env_path) else []
            updated = False
            new_lines = []
            for line in lines:
                if line.strip().startswith("SITE_PASSWORD="):
                    new_lines.append(f"SITE_PASSWORD={new_pw}\n")
                    updated = True
                else:
                    new_lines.append(line)
            if not updated:
                new_lines.append(f"SITE_PASSWORD={new_pw}\n")
            with open(env_path, "w") as f:
                f.writelines(new_lines)
            SITE_PASSWORD = new_pw
            msg = "비밀번호가 변경되었습니다"
    return render_template("change_password.html", msg=msg, error=error)


@app.route("/")
@login_required
def index():
    return render_template("index.html",
                           cl_codes=CL_CODES,
                           sido_codes=SIDO_CODES,
                           gbn_codes=GBN_CODES,
                           has_key=bool(API_KEY),
                           chat_model=CHAT_MODEL)


@app.route("/api/cache-status")
@login_required
def cache_status():
    s = CACHE_STATUS
    pct = int(s["pages_done"] / s["pages_total"] * 100) if s["pages_total"] else 0
    # 캐시 파일 정보
    file_info = {}
    if os.path.exists(CACHE_FILE):
        mtime    = os.path.getmtime(CACHE_FILE)
        age_min  = (time.time() - mtime) / 60
        import datetime
        updated_at = datetime.datetime.fromtimestamp(mtime).strftime("%Y.%m.%d %H:%M")
        file_info = {
            "exists":    True,
            "ageMins":   int(age_min),
            "sizeMB":    round(os.path.getsize(CACHE_FILE) / 1024 / 1024, 1),
            "updatedAt": updated_at,
        }
    return jsonify({
        "state":      s["state"],
        "pct":        pct,
        "count":      s["count"],
        "pagesDone":  s["pages_done"],
        "pagesTotal": s["pages_total"],
        "error":      s["error"],
        "file":       file_info,
    })


@app.route("/api/search")
@login_required
def search():
    item_nm    = request.args.get("itemNm",   "").strip()
    sido_cd    = request.args.get("sidoCd",   "").strip()
    cl_cd      = request.args.get("clCd",     "").strip()
    yadm_nm    = request.args.get("yadmNm",   "").strip()
    gbn_cd     = request.args.get("gbnCd",    "").strip()
    npay_cd_q  = request.args.get("ediCd",    "").strip()
    page       = int(request.args.get("pageNo",    1))
    num_rows   = int(request.args.get("numOfRows", 20))

    # ── 캐시 완료 → 즉시 메모리 검색 ──
    if CACHE_STATUS["state"] == "ready":
        items, total = search_cache(item_nm, sido_cd, cl_cd, yadm_nm, gbn_cd, npay_cd_q, page, num_rows)
        return jsonify({"totalCount": total, "items": items,
                        "filteredCount": total, "fromCache": True,
                        "cacheCount": CACHE_STATUS["count"]})

    # ── 캐시 로딩 중 → 부분 캐시로 빠른 답변 ──
    if CACHE_STATUS["state"] == "loading" and CACHE_STATUS["count"] > 0:
        items, total = search_cache(item_nm, sido_cd, cl_cd, yadm_nm, gbn_cd, npay_cd_q, page, num_rows)
        return jsonify({"totalCount": total, "items": items,
                        "filteredCount": total, "fromCache": True,
                        "cachePartial": True,
                        "cacheCount": CACHE_STATUS["count"],
                        "cachePct": int(CACHE_STATUS["pages_done"] /
                                        max(CACHE_STATUS["pages_total"], 1) * 100)})

    # ── API 키 없음 → 샘플 데이터 ──
    if not API_KEY:
        items, total = search_mock(item_nm, sido_cd, cl_cd, yadm_nm, gbn_cd, page, num_rows)
        return jsonify({"totalCount": total, "items": items,
                        "filteredCount": len(items), "isMock": True})

    # ── 캐시 준비 안 됨 → 직접 API 호출 (제한적) ──
    try:
        base_params = {"serviceKey": API_KEY, "pageNo": page, "numOfRows": num_rows}
        if sido_cd:  base_params["sidoCd"] = sido_cd
        if cl_cd:    base_params["clCd"]   = cl_cd
        if yadm_nm:  base_params["yadmNm"] = yadm_nm
        res = requests.get(f"{BASE_URL}/{OPERATION}", params=base_params, timeout=25)
        res.raise_for_status()
        items, total = parse_xml_items(res.text)
        if item_nm:
            kw = item_nm.lower()
            items = [it for it in items if kw in it["itemNm"].lower() or kw in it["itemNmHosp"].lower()]
        if gbn_cd:
            items = [it for it in items if it["gbnCd"] == gbn_cd]
        return jsonify({"totalCount": total, "items": items, "filteredCount": len(items)})
    except Exception as e:
        items, total = search_mock(item_nm, sido_cd, cl_cd, yadm_nm, gbn_cd, page, num_rows)
        return jsonify({"totalCount": total, "items": items,
                        "filteredCount": len(items), "isMock": True, "apiError": str(e)})


@app.route("/api/items")
@login_required
def item_list():
    """비급여 항목 목록 (자동완성용)"""
    if not API_KEY:
        return jsonify({"items": MOCK_ITEM_NAMES})

    return jsonify({"items": MOCK_ITEM_NAMES})


@app.route("/api/chat", methods=["POST"])
@login_required
def chat():
    body     = request.get_json(force=True)
    messages = body.get("messages", [])
    if not messages or messages[0].get("role") != "system":
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages

    payload = {
        "model":   CHAT_MODEL,
        "messages": messages,
        "stream":  True,
        "options": {"temperature": 0.7},
    }

    def generate():
        try:
            with requests.post(f"{OLLAMA_URL}/api/chat",
                               json=payload, stream=True, timeout=(5, 60)) as r:
                for line in r.iter_lines():
                    if not line:
                        continue
                    decoded = line.decode("utf-8")
                    try:
                        obj = json.loads(decoded)
                        if "error" in obj:
                            yield json.dumps({"error": obj["error"], "done": True}) + "\n"
                            return
                    except Exception:
                        pass
                    yield decoded + "\n"
        except requests.exceptions.Timeout:
            yield json.dumps({"error": "응답 시간이 초과됐습니다. 잠시 후 다시 시도해주세요.", "done": True}) + "\n"
        except requests.exceptions.ConnectionError:
            yield json.dumps({"error": "챗봇 서버에 연결할 수 없습니다.", "done": True}) + "\n"
        except Exception as e:
            yield json.dumps({"error": str(e), "done": True}) + "\n"

    return Response(stream_with_context(generate()),
                    content_type="application/x-ndjson")


@app.route("/suga")
@login_required
def suga_page():
    return render_template("suga.html", chat_model=CHAT_MODEL)


@app.route("/api/suga-search")
@login_required
def suga_search_api():
    keyword = request.args.get("q", "").strip()
    gbn     = request.args.get("gbn", "").strip() or None
    limit   = min(int(request.args.get("limit", 30)), 100)
    results = search_suga(keyword, gbn, limit)
    return jsonify({"count": len(results), "items": results})


@app.route("/api/suga-chat", methods=["POST"])
@login_required
def suga_chat():
    body     = request.get_json(force=True)
    messages = body.get("messages", [])
    user_msg = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")

    suga_context = ""
    drug_context = ""
    if user_msg:
        # 수가 DB 검색
        results = search_suga(user_msg, limit=20)
        if results:
            if len(results) <= 8:
                lines = []
                for r in results:
                    ins = r.get("보험단가", 0)
                    san = r.get("산재단가", 0)
                    jab = r.get("자보단가", 0)
                    gen = r.get("일반단가", 0)
                    lines.append(
                        f"- [{r.get('구분','')}] 수가코드:{r.get('수가코드','')} "
                        f"EDI:{r.get('EDICODE','')} 명칭:{r.get('명칭','')} "
                        f"보험:{ins:,}원 산재:{san:,}원 자보:{jab:,}원 일반:{gen:,}원 "
                        f"분류:{r.get('대분류명칭','')}"
                    )
                suga_context = "\n\n[수가 DB 검색 결과]\n" + "\n".join(lines)
            else:
                names = list(dict.fromkeys(r.get("명칭", "") for r in results[:15]))
                suga_context = (
                    f"\n\n[수가 DB 검색 결과: {len(results)}건 — 일부만 표시]\n"
                    + "\n".join(f"- {n}" for n in names)
                    + "\n→ 항목이 많습니다. 구체적인 명칭이나 수가코드로 다시 질문해 주세요."
                )

        # 약제 질문이면 식약처 API 추가 조회
        drug_keywords = ["인정기준", "급여기준", "보험기준", "효능", "효과", "용법", "용량",
                         "주의사항", "부작용", "금기", "적응증", "약제", "약품"]
        is_drug_query = any(kw in user_msg for kw in drug_keywords) or (
            results and any(r.get("구분") == "약제" for r in results[:3])
        )
        if is_drug_query:
            # 수가 결과 약품명 → 원문 순서로 MFDS 검색 시도
            drug_candidates = []
            if results:
                drug_candidates.append(results[0].get("명칭", ""))
            drug_candidates.append(user_msg)
            mfds_results = []
            for cand in drug_candidates:
                mfds_results = search_mfds_drug(cand, limit=3)
                if mfds_results:
                    break
            if mfds_results:
                lines = []
                for d in mfds_results:
                    lines.append(f"\n[약품명] {d['약품명']}")
                    if d["효능효과"]: lines.append(f"[효능효과] {d['효능효과']}")
                    if d["용법용량"]: lines.append(f"[용법용량] {d['용법용량']}")
                    if d["주의사항"]: lines.append(f"[주의사항] {d['주의사항']}")
                drug_context = "\n\n[식약처 의약품 정보]\n" + "\n".join(lines)

    sys_content = SUGA_SYSTEM_PROMPT + suga_context + drug_context
    full_messages = [{"role": "system", "content": sys_content}] + [
        m for m in messages if m.get("role") != "system"
    ]

    payload = {
        "model":    CHAT_MODEL,
        "messages": full_messages,
        "stream":   True,
        "options":  {"temperature": 0.3},
    }

    def generate():
        try:
            with requests.post(f"{OLLAMA_URL}/api/chat",
                               json=payload, stream=True, timeout=(5, 90)) as r:
                for line in r.iter_lines():
                    if not line:
                        continue
                    decoded = line.decode("utf-8")
                    try:
                        obj = json.loads(decoded)
                        if "error" in obj:
                            yield json.dumps({"error": obj["error"], "done": True}) + "\n"
                            return
                    except Exception:
                        pass
                    yield decoded + "\n"
        except requests.exceptions.Timeout:
            yield json.dumps({"error": f"AI 서버 응답 시간 초과 ({OLLAMA_URL}). 잠시 후 다시 시도해주세요.", "done": True}) + "\n"
        except requests.exceptions.ConnectionError:
            yield json.dumps({"error": f"AI 서버에 연결할 수 없습니다 ({OLLAMA_URL}). 네트워크 또는 서버 상태를 확인해주세요.", "done": True}) + "\n"
        except Exception as e:
            yield json.dumps({"error": str(e), "done": True}) + "\n"

    return Response(stream_with_context(generate()), content_type="application/x-ndjson")


@app.route("/api/models")
@login_required
def models():
    try:
        res  = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
        data = res.json()
        names = [m["name"] for m in data.get("models", [])]
        return jsonify({"models": names, "current": CHAT_MODEL})
    except Exception as e:
        return jsonify({"models": [], "error": str(e)})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    # 수가 데이터 로드 (빠름 — 동기)
    load_suga_data()
    # 전체 캐시 백그라운드 로딩
    if API_KEY:
        t = threading.Thread(target=build_cache, daemon=True)
        t.start()
    print(f"\n✅  http://localhost:{port}  으로 접속하세요\n")
    app.run(debug=False, port=port, host="0.0.0.0")

import os
import re
import json
import time
import random
import requests
import urllib.parse
import gspread
from google import genai
from fastapi import FastAPI, BackgroundTasks, HTTPException

app = FastAPI()

# ==========================================
# CẤU HÌNH HỆ THỐNG & API ENVIRONMENT SETTINGS
# ==========================================
API_KEY_SHEET_ID = "1wzgeUWKlXe-QU-rDZLaLjIQxeXreNvbm3Fi88UZjXWM"
DATA_SHEET_ID = "1YrgKGSUsPTBMxm39qeM8QRxalhfLQD3Zg8gozx6KTgM"
CX_LINKEDIN = "a6be6e8ccdb58403b"
SECRET_TOKEN = "MySuperSecretToken123"

CHECK_DELAY = 0.5
SEARCH_DELAY = 1.5
GEMINI_DELAY_BASE = 4.0
GEMINI_DELAY_JITTER = 1.5

def sleep_with_jitter(base=GEMINI_DELAY_BASE, jitter=GEMINI_DELAY_JITTER):
    time.sleep(base + random.uniform(0, jitter))

# ==========================================
# HÀM KHỞI TẠO GSPREAD & HELPER RETRY GOOGLE SHEETS
# ==========================================
def get_gspread_client():
    service_account_info = os.getenv("SERVICE_ACCOUNT_JSON")
    if service_account_info:
        try:
            creds_dict = json.loads(service_account_info)
            return gspread.service_account_from_dict(creds_dict)
        except Exception as e:
            print(f"❌ Lỗi parse SERVICE_ACCOUNT_JSON từ môi trường: {e}")
            raise e
    else:
        print("⚠️ Không thấy SERVICE_ACCOUNT_JSON trong môi trường, thử tìm file service_account.json local...")
        return gspread.service_account(filename="service_account.json")

def retry_gspread_call(fn, *args, max_retries=5, initial_wait=3, **kwargs):
    """Bọc các lệnh Google Sheets, tự động retry nếu dính 503, 500 hoặc 429."""
    wait_sec = initial_wait
    for attempt in range(1, max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            err_str = str(e)
            if any(code in err_str for code in ["503", "500", "429", "Quota exceeded", "unavailable"]):
                if attempt == max_retries:
                    raise e
                print(f" ⏳ [GSpread {err_str[:35]}...] Thử lại lần {attempt}/{max_retries} sau {wait_sec}s...")
                time.sleep(wait_sec + random.uniform(0.5, 1.5))
                wait_sec = min(wait_sec * 2, 60)
            else:
                raise e

def safe_sheet_update(sheet, range_name, values, max_retries=5):
    return retry_gspread_call(sheet.update, range_name=range_name, values=values, max_retries=max_retries)

# ==========================================
# CLASS QUẢN LÝ KEY ROTATION (ĐA TAB)
# ==========================================
class MultiTabKeyManager:
    def __init__(self, sheet, key_type="KEY"):
        self._sheet = sheet
        self._type = key_type
        self._keys = []
        self._idx = 0
        self._clients = {}

    def load(self):
        rows = retry_gspread_call(self._sheet.get_all_values)
        self._keys = []
        for i, r in enumerate(rows[3:]):
            row_num = i + 4
            if r and r[0].strip():
                status = r[1].strip() if len(r) > 1 else ""
                if status in ["Mã API hết lượt", "API het luot", "Key loi (401)"]:
                    continue
                self._keys.append({"key": r[0].strip(), "row": row_num})
        print(f"📊 [{self._type}] Đã nạp {len(self._keys)} key khả dụng từ tab '{self._sheet.title}'.")

    def current(self):
        return self._keys[self._idx] if self._idx < len(self._keys) else None

    def current_key(self):
        item = self.current()
        return item["key"] if item else None

    def get_client(self):
        key = self.current_key()
        if key is None:
            return None
        if key not in self._clients:
            self._clients[key] = genai.Client(api_key=key)
        return self._clients[key]

    def exhaust(self):
        if self._idx < len(self._keys):
            self._mark(self._keys[self._idx]["row"], "429")
            self._idx += 1

    def invalidate(self):
        if self._idx < len(self._keys):
            self._mark(self._keys[self._idx]["row"], "401")
            self._idx += 1

    def _mark(self, row, kind):
        msg = "Key loi (401)" if kind == "401" else "API het luot"
        try:
            safe_sheet_update(self._sheet, f"B{row}", [[msg]])
            print(f"🛑 [{self._type}] Hàng {row} đánh dấu: {msg}")
        except Exception as e:
            print(f"⚠️ Lỗi đánh dấu key hàng {row}: {e}")

# ==========================================
# HÀM AI XÁC MINH & TRA CỨU
# ==========================================
def verify_ceo_with_ai(company_name, name, job, url, gemini_mgr):
    prompt = f"""Nhiệm vụ: Xác minh xem người này có phải là CEO hoặc Founder của công ty không, đồng thời chuẩn hóa lại chức vụ.

Công ty cần tìm: {company_name}
Người tìm thấy: {name}
Chức vụ theo Google: {job}
LinkedIn URL: {url}

Đánh giá:
1. Tên công ty trong URL LinkedIn có khớp với công ty cần tìm không?
2. Chức vụ có phải là CEO, Founder, Co-founder, Director, hoặc tương đương không?
3. Tên người tìm thấy có hợp lệ không (không phải từ khóa tìm kiếm)?
4. Nếu "Chức vụ theo Google" thực chất là địa điểm/không liên quan, hãy suy luận trả về chức danh đúng.

Trả lời JSON thuần:
{{"verified": true/false, "confidence": "cao/trung bình/thấp", "reason": "lý do ngắn gọn 1 câu", "job_title": "chức vụ chuẩn hóa"}}"""

    attempt = 0
    backoff = 4
    while attempt < 4:
        attempt += 1
        if not gemini_mgr.current_key():
            return {"verified": False, "confidence": "thấp", "reason": "Hết key AI", "job_title": job}

        try:
            client = gemini_mgr.get_client()
            response = client.models.generate_content(model="gemini-flash-latest", contents=prompt)
            text = response.text.strip()
            match = re.search(r'\{.*?\}', text, re.DOTALL)
            if match:
                result = json.loads(match.group())
                if not result.get("job_title"):
                    result["job_title"] = job
                return result
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                gemini_mgr.exhaust()
                continue
            if "401" in err_str or "API_KEY_INVALID" in err_str:
                gemini_mgr.invalidate()
                continue
            if "503" in err_str or "UNAVAILABLE" in err_str:
                time.sleep(backoff + random.uniform(0, 3))
                backoff = min(backoff * 2, 60)
                continue
            break
    return {"verified": False, "confidence": "thấp", "reason": "Lỗi AI", "job_title": job}

def get_location_gemini(ceo_name, company, linkedin_url, gemini_mgr):
    prompt = (
        f"What city and state/country does '{ceo_name}', "
        f"the CEO/Founder of '{company}' (LinkedIn: {linkedin_url}), "
        f"currently live in or work from? "
        f"Reply with ONLY city and state/country, example: 'San Francisco, CA'. "
        f"If unknown, reply '-'."
    )
    attempt = 0
    backoff = 4
    while attempt < 4:
        attempt += 1
        if not gemini_mgr.current_key():
            return "-"

        try:
            client = gemini_mgr.get_client()
            response = client.models.generate_content(model="gemini-flash-latest", contents=prompt)
            return response.text.strip() if response.text else "-"
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                gemini_mgr.exhaust()
                continue
            if "401" in err_str or "API_KEY_INVALID" in err_str:
                gemini_mgr.invalidate()
                continue
            if "503" in err_str or "UNAVAILABLE" in err_str:
                time.sleep(backoff + random.uniform(0, 3))
                backoff = min(backoff * 2, 60)
                continue
            break
    return "-"

def is_high_confidence(status_text):
    if not status_text:
        return False
    text = status_text.strip().lower()
    return ("xác nhận" in text and "không xác nhận" not in text) and "(cao)" in text

# ==========================================
# MAIN AUTOMATION WORKFLOW
# ==========================================
def run_automation_logic():
    print("🚀 Bắt đầu kiểm tra tiến trình tự động...")
    try:
        gc = get_gspread_client()
        data_sheet = retry_gspread_call(lambda: gc.open_by_key(DATA_SHEET_ID).worksheet("search example"))
        data_matrix = retry_gspread_call(data_sheet.get_all_values)
        rows = data_matrix[1:]

        # 1. KIỂM TRA TRƯỚC XEM CÓ DATA MỚI CẦN XỬ LÝ KHÔNG (TIẾT KIỆM TỐI ĐA)
        todo_search = []
        todo_location = []

        for i, row in enumerate(rows):
            row_idx = i + 2
            company = row[0].strip() if len(row) > 0 else ""
            col_b = row[1].strip() if len(row) > 1 else ""
            col_e = row[4].strip() if len(row) > 4 else ""
            col_f = row[5].strip() if len(row) > 5 else ""
            location = row[6].strip() if len(row) > 6 else ""

            # Nhánh 1: Cần tìm CEO Profile (Dòng có tên Cty nhưng Cột B, E, F đều chưa có kết quả)
            if company and col_b == "" and col_e == "" and col_f == "":
                todo_search.append({"idx": row_idx, "name": company})
            
            # Nhánh 2: Cần tìm Location (Đã xác nhận cao nhưng Cột G chưa có kết quả)
            elif is_high_confidence(col_e) and location == "":
                todo_location.append({
                    "idx": row_idx,
                    "company": company,
                    "url": col_b,
                    "name": row[2].strip() if len(row) > 2 else ""
                })

        # NẾU HOÀN TOÀN KHÔNG CÓ DỮ LIỆU MỚI THÌ THOÁT NGAY
        if not todo_search and not todo_location:
            print("💤 Không có dữ liệu mới nào cần chạy. Kết thúc để tránh tốn API.")
            return

        # Nạp Key Sheets
        gemini_sheet = retry_gspread_call(lambda: gc.open_by_key(API_KEY_SHEET_ID).worksheet("Gemini API"))
        api_sheet = retry_gspread_call(lambda: gc.open_by_key(API_KEY_SHEET_ID).worksheet("Custom Search API"))

        search_key_mgr = MultiTabKeyManager(api_sheet, "SEARCH")
        search_key_mgr.load()

        gemini_key_mgr = MultiTabKeyManager(gemini_sheet, "GEMINI")
        gemini_key_mgr.load()

        # ==========================================
        # BƯỚC 1: XỬ LÝ CEO PROFILE
        # ==========================================
        if todo_search:
            print(f"🚀 [PHẦN 1] Phát hiện {len(todo_search)} dòng mới cần tìm CEO Profile...")
            for task in todo_search:
                row_idx = task["idx"]
                company_query = task["name"]

                while True:
                    api_obj = search_key_mgr.current()
                    if not api_obj:
                        print("🛑 ĐÃ HẾT TOÀN BỘ KEY SEARCH! Dừng Bước 1.")
                        todo_search = []  # Dừng xử lý các dòng còn lại
                        break

                    api_key = api_obj['key']
                    query = f'"{company_query}" (CEO OR Founder) site:linkedin.com/in/'
                    url = f"https://www.googleapis.com/customsearch/v1?q={urllib.parse.quote(query)}&key={api_key}&cx={CX_LINKEDIN}"

                    try:
                        res = requests.get(url, timeout=10)
                        data = res.json()

                        if res.status_code in [403, 429] or "dailyLimitExceeded" in str(data):
                            search_key_mgr.exhaust()
                            continue
                        if res.status_code == 401:
                            search_key_mgr.invalidate()
                            continue

                        items = data.get("items", [])
                        if items:
                            item = items[0]
                            link = item.get("link", "-")
                            full_title = item.get("title", "")
                            clean_title = full_title.split("|")[0].split("...")[0].strip()
                            parts = [p.strip() for p in clean_title.split("-")]
                            name = parts[0] if len(parts) > 0 else "-"
                            job_raw = " - ".join(parts[1:]) if len(parts) > 1 else "-"

                            ai_result = verify_ceo_with_ai(company_query, name, job_raw, link, gemini_key_mgr)

                            ai_status = "✅ Xác nhận" if ai_result.get("verified") is True else ("❌ Không xác nhận" if ai_result.get("verified") is False else "⚠️ Không rõ")
                            ai_confidence = ai_result.get("confidence", "-")
                            ai_reason = ai_result.get("reason", "-")
                            job_final = ai_result.get("job_title", job_raw) if (ai_result.get("verified") is True and ai_confidence.strip().lower() == "cao") else job_raw

                            payload = [link, name, job_final, f"{ai_status} ({ai_confidence})", ai_reason]
                            safe_sheet_update(data_sheet, f"B{row_idx}:F{row_idx}", [payload])
                            print(f" ✅ [{row_idx}] Cập nhật B:F cho {company_query}")

                            # Nếu xác nhận cao, kiểm tra và lấy luôn location để đỡ phải đợi quét lại
                            if is_high_confidence(f"{ai_status} ({ai_confidence})"):
                                loc = get_location_gemini(name, company_query, link, gemini_key_mgr)
                                safe_sheet_update(data_sheet, f"G{row_idx}", [[loc]])
                                print(f" 📍 [{row_idx}] Điền luôn Location (G): {loc}")

                        else:
                            # ĐÓNG DÒNG ĐẦY ĐỦ ĐỂ LẦN SAU KHÔNG BỊ QUÉT LẠI
                            payload = ["- Không tìm thấy", "-", "-", "❌ Không tìm thấy", "Không có kết quả Google Search", "-"]
                            safe_sheet_update(data_sheet, f"B{row_idx}:G{row_idx}", [payload])
                            print(f" ➖ [{row_idx}] Không tìm thấy CEO cho {company_query}. Đã chốt dòng.")

                        sleep_with_jitter()
                        break

                    except Exception as e:
                        print(f"❌ Lỗi xử lý dòng {row_idx}: {e}")
                        break

        # ==========================================
        # BƯỚC 2: XỬ LÝ LOCATION CÁC DÒNG CÒN SÓT
        # ==========================================
        if todo_location:
            print(f"\n🚀 [PHẦN 2] Phát hiện {len(todo_location)} dòng cần bổ sung Vị trí CEO (Cột G)...")
            for task in todo_location:
                if not gemini_key_mgr.current_key():
                    print("🛑 ĐÃ HẾT TOÀN BỘ KEY GEMINI! Dừng Bước 2.")
                    break

                row_idx = task["idx"]
                ceo_name = task["name"]

                if not ceo_name or ceo_name == "-":
                    safe_sheet_update(data_sheet, f"G{row_idx}", [["-"]])
                    continue

                location = get_location_gemini(ceo_name, task["company"], task["url"], gemini_key_mgr)
                safe_sheet_update(data_sheet, f"G{row_idx}", [[location]])
                print(f" 📍 [{row_idx}] Updated CEO Location (G): {location}")
                sleep_with_jitter()

        print("\n🏁 HOÀN TẤT TIẾN TRÌNH.")

    except Exception as general_err:
        print(f"❌ Lỗi: {general_err}")

# ==========================================
# ENDPOINT FASTAPI FOR CRON-JOB.ORG
# ==========================================
@app.get("/")
def home():
    return {"status": "Service is running!"}

@app.get("/run-job")
def trigger_job(background_tasks: BackgroundTasks, token: str = ""):
    if token != SECRET_TOKEN:
        raise HTTPException(status_code=403, detail="Unauthorized")

    background_tasks.add_task(run_automation_logic)
    return {"message": "Job successfully triggered in background!"}

if __name__ == "__main__":
    run_automation_logic()

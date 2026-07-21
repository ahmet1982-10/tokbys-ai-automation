import re
import os
import time
import json
import logging
import unicodedata
from pathlib import Path

import pyautogui
pyautogui.FAILSAFE = False
import pyperclip
from pypdf import PdfReader

BASE_DIR = os.path.expanduser("~")
OUTPUT_ROOT = os.path.join(BASE_DIR, "TOKBYS_DEKONT")
os.makedirs(OUTPUT_ROOT, exist_ok=True)

# --- Config ---
CONFIG = {
    # Paths
    "pdf_path": os.path.join(BASE_DIR, "YandexDisk", "OTOMASYON", "dekontlar.pdf"),
    "output_root": OUTPUT_ROOT,
    "preview_csv": os.path.join(OUTPUT_ROOT, "dekont_preview.csv"),
    "log_path": os.path.join(OUTPUT_ROOT, "tokbys_dekont.log"),

    # Parsing/formatting
    "tutar_kaynagi": "toplam",          # "toplam" or "anapara"
    "dekont_no_kaynagi": "bim_ref",     # "bim_ref" or "islem_tarihi"
    "use_decimal_comma": True,           # True => 1.250,00  |  False => 1250.00
    "date_format": "dd.mm.yyyy",        # "dd.mm.yyyy" or "dd/mm/yyyy"
    "dekont_tarihi_kaynagi": "pdf",   # "pdf" or "onceki_is_gunu"
    "prompt_dekont_tarihi": True,     # True => CSV oncesi tarih sor

    # Progress/resume
    "progress_path": os.path.join(OUTPUT_ROOT, "tokbys_progress.json"),
    "result_csv": os.path.join(OUTPUT_ROOT, "tokbys_sonuc.csv"),
    "resume_from_progress": True,

    # Automation flow
    "dry_run": False,                     # True => sadece parse + CSV, otomasyon yapmaz
    "start_index": 0,                    # kaldığı yerden devam için
    "taksit_secim": "skip",     # "ilk_odenmedi" or "secili_satir"
    "checkbox_clicker_entegre": True,     # True => lightblue satirdaki TaksitCB otomatik secilir
    "focus_browser_before_js": True,      # True => JS yapistirmadan once Firefox'u one alir
    "browser_window_titles": ["Firefox", "Mozilla Firefox"],

    # Screen coordinates (adjust if needed)
    "coords": {
        "kredi_no_field": (300, 228),
        "krediye_git": (281, 251),
        "page_focus": (294, 259),
        "console_focus": (80, 998),
	"tahsilat_icon": (340, 767),
	"date_input": (487, 224),
        "iframe_close": (1856, 102),
        "islem_yeri_detay_open": (316, 229),
        "islem_yeri_detay_option": (353, 266),
        "dekont_no_input": (553, 222),
    },
}

logger = logging.getLogger("tokbys")
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(message)s')

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

file_handler = logging.FileHandler(CONFIG["log_path"], encoding="utf-8")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)


def tarayici_penceresine_gec() -> bool:
    if not CONFIG.get("focus_browser_before_js"):
        return False
    try:
        get_windows = getattr(pyautogui, "getWindowsWithTitle", None)
        if not get_windows:
            return False
        for title in CONFIG.get("browser_window_titles", []):
            for win in get_windows(title):
                try:
                    if getattr(win, "isMinimized", False):
                        win.restore()
                    win.activate()
                    time.sleep(0.3)
                    return True
                except Exception:
                    continue
    except Exception:
        pass
    return False


def terminal_girdi_tamponunu_temizle() -> None:
    try:
        import msvcrt
        time.sleep(0.1)
        while msvcrt.kbhit():
            msvcrt.getwch()
    except Exception:
        pass


def konsolu_hazirla() -> None:
    tarayici_penceresine_gec()
    px, py = CONFIG["coords"].get("page_focus", (0, 0))
    if px and py:
        pyautogui.click(px, py)
        time.sleep(0.2)
    pyautogui.hotkey("ctrl", "shift", "k")
    time.sleep(0.8)
    cx, cy = CONFIG["coords"].get("console_focus", (0, 0))
    if cx and cy:
        pyautogui.click(cx, cy)
        time.sleep(0.2)


def build_output_dir_name(date_str: str) -> str:
    s = (date_str or '').strip().replace('/', '.').replace('-', '.')
    parts = [p for p in s.split('.') if p]
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        gun, ay, yil = parts
        return f"{gun.zfill(2)}{ay.zfill(2)}{yil.zfill(4)}"
    return time.strftime('%d%m%Y')


def configure_output_paths(date_str: str) -> str:
    global file_handler
    folder_name = build_output_dir_name(date_str)
    output_root = CONFIG.get('output_root') or OUTPUT_ROOT
    output_dir = os.path.join(output_root, folder_name)
    os.makedirs(output_dir, exist_ok=True)

    CONFIG['preview_csv'] = os.path.join(output_dir, 'dekont_preview.csv')
    CONFIG['result_csv'] = os.path.join(output_dir, 'tokbys_sonuc.csv')
    CONFIG['progress_path'] = os.path.join(output_dir, 'tokbys_progress.json')
    CONFIG['log_path'] = os.path.join(output_dir, 'tokbys_dekont.log')

    try:
        logger.removeHandler(file_handler)
        file_handler.close()
    except Exception:
        pass

    file_handler = logging.FileHandler(CONFIG['log_path'], encoding='utf-8')
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return output_dir

# --- Helpers ---

def normalize_text(s: str) -> str:
    # Turkish chars -> ASCII; keeps numbers and punctuation
    return ''.join(
        c for c in unicodedata.normalize('NFKD', s)
        if not unicodedata.combining(c)
    )


def first_match(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(1).strip() if m else None


def extract_kp_no(text: str) -> str:
    # Try KREDI HESAP NO like 1740-KP004168 -> KP004168
    m = re.search(r"KREDI HESAP NO\s*:\s*[0-9]+-?(KP\d+)", text, re.IGNORECASE)
    if m:
        return m.group(1)
    # Fallback: any KP\d+ in text (e.g., ACIKLAMA)
    m = re.search(r"KP\d+", text, re.IGNORECASE)
    return m.group(0).upper() if m else ""




def extract_bim_ref_no(s: str | None) -> str:
    if not s:
        return ""
    nums = re.findall(r"\d{4,}", s)
    return nums[-1] if nums else s




def extract_bim_ref_last6(s: str | None) -> str:
    if not s:
        return ""
    digits = re.findall(r"\d", s)
    if not digits:
        return s
    d = "".join(digits)
    return d[-6:] if len(d) >= 6 else d


def parse_amount(s: str | None) -> float:
    if not s:
        return 0.0
    # extract first numeric token (allows thousands/decimal)
    m = re.search(r"-?[0-9][0-9.,]*", s)
    if not m:
        return 0.0
    s = m.group(0).replace(" ", "")
    if "," in s and "." in s:
        # decide thousand/decimal by order
        if s.find(",") < s.find("."):
            s = s.replace(",", "")
        else:
            s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    return float(s)

def format_amount(value: float, use_decimal_comma: bool) -> str:
    if use_decimal_comma:
        s = f"{value:,.2f}"
        # 1,234.56 -> 1.234,56
        s = s.replace(",", "X").replace(".", ",").replace("X", ".")
        return s
    return f"{value:.2f}"


def format_date(ddmmyyyy: str, date_format: str) -> str:
    if not ddmmyyyy:
        return ""
    if date_format == "dd.mm.yyyy":
        return ddmmyyyy.replace("/", ".")
    return ddmmyyyy


def prev_business_day(date_str: str) -> str:
    # date_str in dd.mm.yyyy or dd/mm/yyyy
    if not date_str:
        return ""
    parts = date_str.replace('/', '.').split('.')
    if len(parts) != 3:
        return date_str
    d, m, y = map(int, parts)
    import datetime as dt
    cur = dt.date(y, m, d)
    cur -= dt.timedelta(days=1)
    while cur.weekday() >= 5:
        cur -= dt.timedelta(days=1)
    return f"{cur.day:02d}.{cur.month:02d}.{cur.year}"


# --- PDF parsing ---

def parse_dekonts(pdf_path: str) -> list[dict]:
    reader = PdfReader(pdf_path)
    rows: list[dict] = []

    for idx, page in enumerate(reader.pages, start=1):
        raw_text = page.extract_text() or ""
        text = normalize_text(raw_text)

        kredi_no = first_match(r"KREDI HESAP NO\s*:\s*([0-9]{4}-[A-Z]{2}[0-9]{6})", text)
        kredi_no_short = extract_kp_no(text)
        islem_tarihi = first_match(r"ISLEM TARIHI\s*:\s*(\d{2}/\d{2}/\d{4})", text)
        bim_ref = (
            first_match(r"BIM\s*REF(?:[^:\r\n]*)?\s*:\s*([A-Z0-9.\-]+)", text)
            or first_match(r"BIMREF(?:[^:\r\n]*)?\s*:\s*([A-Z0-9.\-]+)", text)
            or first_match(r"([A-Z]-\d{4}-\d{2}-\d{2}-[0-9.]+)", text)
        )
        bim_ref_no = extract_bim_ref_no(bim_ref)
        bim_ref_last6 = extract_bim_ref_last6(bim_ref)

        anapara = parse_amount(first_match(r"ANAPARA TUTARI \(TL\)\s*:\s*([0-9.,]+)", text))
        faiz = parse_amount(first_match(r"FAIZ TUTARI \(TL\)\s*:\s*([0-9.,]+)", text))
        gecikme = parse_amount(first_match(r"GECIKME TUTARI \(TL\)\s*:\s*([0-9.,]+)", text))
        komisyon_text = first_match(r"BANKA KOMISYONU \(TL\)\s*:\s*([0-9.,]+)", text)
        komisyon = parse_amount(komisyon_text)
        koop_masraf = parse_amount(first_match(r"KOOP\. MASRAF KARSILIGI \(TL\)\s*:\s*([0-9.,]+)", text))
        toplam = parse_amount(first_match(r"TOPLAM \(TL\s*\)\s*:\s*([0-9.,]+)", text))

        if CONFIG["tutar_kaynagi"] == "anapara":
            tutar = anapara
        else:
            tutar = toplam if toplam else anapara

        if CONFIG["dekont_no_kaynagi"] == "islem_tarihi":
            dekont_no = islem_tarihi or (bim_ref or "")
        else:
            dekont_no = bim_ref_last6 or (bim_ref_no or (bim_ref or ""))

        rows.append({
            "page": idx,
            "kredi_no": kredi_no or "",
            "kredi_no_short": kredi_no_short or "",
            "tarih": (
                format_date(islem_tarihi or "", CONFIG["date_format"])
                if CONFIG.get("dekont_tarihi_kaynagi") == "pdf"
                else prev_business_day(format_date(islem_tarihi or "", CONFIG["date_format"]))
            ),
            "dekont_no": dekont_no,
            "tutar": format_amount(tutar, CONFIG["use_decimal_comma"]),
            "banka_faizi": format_amount(faiz, CONFIG["use_decimal_comma"]),
            "gecikme_faizi": format_amount(gecikme, CONFIG["use_decimal_comma"]),
            "masraf": format_amount(koop_masraf, CONFIG["use_decimal_comma"]),
            "banka_komisyon": format_amount(komisyon, CONFIG["use_decimal_comma"]),
            "banka_komisyon_dekontta_var": komisyon_text is not None,
            "anapara_raw": anapara,
            "koop_masraf_raw": koop_masraf,
        })

    return rows


def write_preview_csv(rows: list[dict], path: str) -> None:
    import csv
    fields = [
        "page",
        "kredi_no",
        "kredi_no_short",
        "tarih",
        "dekont_no",
        "tutar",
        "banka_faizi",
        "gecikme_faizi",
        "masraf",
        "banka_komisyon",
        "banka_komisyon_dekontta_var",
        "anapara_raw",
        "koop_masraf_raw",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)



def load_progress(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8") or "{}")
    except Exception:
        return {}


def save_progress(path: str, data: dict) -> None:
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def append_result(path: str, row: dict) -> None:
    import csv
    p = Path(path)
    exists = p.exists()
    fields = ["kredi_no", "taksit_no", "taksit_durum", "tarih", "dekont_no", "status", "reason"]
    with p.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in fields})


def devam_iste(prompt: str) -> bool:
    try:
        terminal_girdi_tamponunu_temizle()
        cevap = input(f"{prompt} (e/h): ").strip().lower()
    except EOFError:
        return False
    return cevap in {"e", "evet", "y", "yes"}


# --- Automation ---

def konsola_js_yolla(js_kod: str, bekle: float = 2.0) -> None:
    konsolu_hazirla()
    try:
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.1)
        pyautogui.press("backspace")
        time.sleep(0.1)
    except Exception:
        pass
    pyperclip.copy((js_kod or "").strip())
    pyautogui.hotkey("ctrl", "v")
    pyautogui.press("enter")
    time.sleep(bekle)


def konsola_js_oku(js_expr: str, bekle: float = 2.0) -> dict:
    # Uses DevTools console copy(...) to clipboard, then reads clipboard.
    js = f"copy(JSON.stringify({js_expr}))"
    konsola_js_yolla(js, bekle=bekle)
    try:
        return json.loads(pyperclip.paste() or "{}")
    except Exception:
        return {}





def set_local_storage_dekont_date(date_str: str) -> None:
    js = f"localStorage.setItem('dekontDate', '{date_str}')"
    konsola_js_yolla(js, bekle=1)

def kredi_no_yaz(kredi_no: str) -> None:
    # Main path: physical click + paste into kredi no field
    ix, iy = CONFIG["coords"].get("kredi_no_field", (0, 0))
    if ix and iy:
        pyautogui.click(ix, iy)
        time.sleep(0.3)
        try:
            pyautogui.hotkey('ctrl', 'a')
            time.sleep(0.1)
            pyautogui.press('backspace')
            time.sleep(0.1)
        except Exception:
            pass
        pyperclip.copy(kredi_no)
        pyautogui.hotkey('ctrl', 'v')
        time.sleep(3.0)
        pyautogui.press('space')
        time.sleep(0.1)
        pyautogui.press('backspace')
        time.sleep(0.3)

    # Backup path: if field remained blank, set OPKLKrediNo via JS and trigger filter
    js = f"""
(function(){{
  function touch(win){{
    try{{
      const el = win.document.getElementById('OPKLKrediNo');
      if (!el) return false;
      const current = (el.value || '').trim();
      if (!current) {{
        el.focus();
        el.value = '{kredi_no}';
        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
        el.dispatchEvent(new Event('change', {{ bubbles: true }}));
      }}
      try {{ if (typeof win.OpKrediListesiAlFiltreli === 'function') win.OpKrediListesiAlFiltreli(1); }} catch (e) {{}}
      return true;
    }}catch(e){{}}
    return false;
  }}

  if (touch(window)) return true;
  const iframes = Array.from(document.querySelectorAll('iframe'));
  for (const f of iframes){{
    try{{ if (f.contentWindow && touch(f.contentWindow)) return true; }}catch(e){{}}
  }}
  return false;
}})()
"""
    konsola_js_yolla(js, bekle=1)
    logger.info(f"[ok] Kredi No yazildi: {kredi_no}")


def krediye_git() -> bool:
    js = r'''
(function(){
  function fire(el, type){
    try{ el.dispatchEvent(new MouseEvent(type, {bubbles:true,cancelable:true})); }catch(e){}
  }
  function dblClick(el){
    if (!el) return false;
    try{ el.scrollIntoView({block:'center', inline:'center'}); }catch(e){}
    fire(el,'mousedown'); fire(el,'mouseup'); fire(el,'click');
    fire(el,'mousedown'); fire(el,'mouseup'); fire(el,'click');
    fire(el,'dblclick');
    return true;
  }
  function findAndDbl(win){
    try{
      const imgs = Array.from(win.document.querySelectorAll('img'));
      for (const img of imgs){
        const t = ((img.alt||'') + ' ' + (img.title||'') + ' ' + (img.src||'')).toLowerCase();
        if (t.includes('krediye git') || t.includes('clone.png')){
          return dblClick(img);
        }
      }
    }catch(e){}
    return false;
  }
  if (findAndDbl(window)) return true;
  const iframes = Array.from(document.querySelectorAll('iframe'));
  for (const f of iframes){
    try{ if (f.contentWindow && findAndDbl(f.contentWindow)) return true; }catch(e){}
  }
  return false;
})()
'''
    konsola_js_yolla(js, bekle=2)
    if kredi_detay_acik_mi():
        logger.info("[ok] Krediye Git (clone.png) JS ile acildi")
        time.sleep(5)
        return True

    dx, dy = CONFIG["coords"].get("krediye_git", (0, 0))
    if dx and dy:
        pyautogui.doubleClick(dx, dy)
        time.sleep(2)

    if kredi_detay_acik_mi():
        logger.info("[ok] Krediye Git (clone.png) koordinat fallback ile acildi")
        time.sleep(5)
        return True
    else:
        logger.warning("[warn] Krediye Git sonrasi kredi detay ekrani dogrulanamadi")
    return False


def taksitler_tabina_git() -> None:
    js = r'''
(function(){
  function clickTab(win){
    try{
      const els = Array.from(win.document.querySelectorAll('a, td, span, div, li'));
      for (const el of els){
        const t = (el.textContent || '').trim().toLowerCase();
        if (t === 'taksitler'){
          el.click();
          return true;
        }
      }
    }catch(e){}
    return false;
  }
  if (clickTab(window)) return true;
  const iframes = Array.from(document.querySelectorAll('iframe'));
  for (const f of iframes){
    try{ if (f.contentWindow && clickTab(f.contentWindow)) return true; }catch(e){}
  }
  return false;
})()
'''
    konsola_js_yolla(js, bekle=1)
    logger.info('[ok] Taksitler sekmesine tiklandi')
    if CONFIG.get("checkbox_clicker_entegre"):
        time.sleep(1)
        lightblue_taksit_checkbox_sec()



def taksit_sec() -> dict:
    lightblue_secim = lightblue_taksit_checkbox_sec(log_if_missing=False)
    if lightblue_secim.get("found"):
        logger.info(f"[ok] Lightblue checkbox secildi (entegre eklenti mantigi). Sonuc: {lightblue_secim}")
        return lightblue_secim

    # Eklentisiz, tek checkbox secimi: once tum secimleri temizler, sonra 0 (fallback 4/5) secer.
    js_expr = r"""
(function(){
  function isVisible(el){
    try{
      const st = el.ownerDocument.defaultView.getComputedStyle(el);
      return st.display !== 'none' && st.visibility !== 'hidden';
    }catch(e){}
    return true;
  }
  function getCtx(rootWin){
    try{
      const own = Array.from(rootWin.document.querySelectorAll('input[type="checkbox"][rel="TaksitCB"]'));
      if (own.length) return rootWin;
    }catch(e){}
    const iframes = Array.from(document.querySelectorAll('iframe'));
    for (const f of iframes){
      try{
        if (f.contentWindow){
          const cbs = Array.from(f.contentWindow.document.querySelectorAll('input[type="checkbox"][rel="TaksitCB"]'));
          if (cbs.length) return f.contentWindow;
        }
      }catch(e){}
    }
    return rootWin;
  }
  function uncheckAll(cbs){
    for (const cb of cbs){
      try{
        cb.checked = false;
        cb.dispatchEvent(new Event('change', {bubbles:true}));
      }catch(e){}
    }
  }
  function pickOne(cbs, allowed){
    for (const cb of cbs){
      const dur = (cb.getAttribute('taksitdurum') || '').toString().trim();
      if (!allowed.includes(dur)) continue;
      if (!isVisible(cb)) continue;
      const tr = cb.closest('tr');
      if (tr){
        try{ tr.scrollIntoView({block:'center'}); }catch(e){}
      }
      try{
        cb.checked = true;
        cb.click();
      }catch(e){}
      try{
        cb.checked = true;
        cb.dispatchEvent(new Event('change', {bubbles:true}));
      }catch(e){}
      return cb;
    }
    return null;
  }
  const ctx = getCtx(window);
  const cbs = Array.from(ctx.document.querySelectorAll('input[type="checkbox"][rel="TaksitCB"]'));
  uncheckAll(cbs);
  let chosen = pickOne(cbs, ['0']);
  if (!chosen) chosen = pickOne(cbs, ['4','5']);
  const checked = cbs.filter(cb => cb.checked);
  return {
    found: !!chosen,
    checked_count: checked.length,
    taksit_no: chosen ? (chosen.getAttribute('taksitno') || '') : '',
    taksit_durum: chosen ? (chosen.getAttribute('taksitdurum') || '') : '',
    checked_list: checked.map(cb => ({
      taksit_no: cb.getAttribute('taksitno') || '',
      taksit_durum: cb.getAttribute('taksitdurum') || ''
    }))
  };
})()
"""
    info = konsola_js_oku(js_expr, bekle=1)
    logger.info(f"[ok] Taksit secim denendi (tek secim). Sonuc: {info}")
    return info or {}




def lightblue_taksit_checkbox_sec(log_if_missing: bool = True) -> dict:
    js_expr = r"""
(function(){
  function isVisible(el){
    try{
      const st = el.ownerDocument.defaultView.getComputedStyle(el);
      return st.display !== 'none' && st.visibility !== 'hidden';
    }catch(e){}
    return true;
  }
  function isLightBlue(bg){
    bg = (bg || '').toLowerCase().replace(/\s+/g, '');
    return bg === 'rgb(173,216,230)' || bg === 'rgba(173,216,230,1)';
  }
  function selectIn(win){
    try{
      const doc = win.document;
      const rows = Array.from(doc.querySelectorAll('tr'));
      const allCbs = Array.from(doc.querySelectorAll('input[type="checkbox"][rel="TaksitCB"]'));
      for (const row of rows){
        let bg = '';
        try{ bg = win.getComputedStyle(row).backgroundColor || ''; }catch(e){}
        if (!isLightBlue(bg)) continue;
        const checkbox = row.querySelector('input[rel="TaksitCB"][taksitdurum="0"]');
        if (!checkbox || !isVisible(checkbox)){
          return {
            found: false,
            reason: 'lightblue_satirda_odenmedi_checkbox_yok',
            row_text: (row.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 250),
            checkbox_count: allCbs.length
          };
        }
        for (const cb of allCbs){
          try{
            cb.checked = false;
            cb.dispatchEvent(new Event('change', {bubbles:true}));
          }catch(e){}
        }
        try{ row.scrollIntoView({block:'center'}); }catch(e){}
        try{
          checkbox.checked = true;
          checkbox.click();
        }catch(e){}
        try{
          checkbox.checked = true;
          checkbox.dispatchEvent(new Event('change', {bubbles:true}));
        }catch(e){}
        return {
          found: true,
          taksit_no: checkbox.getAttribute('taksitno') || '',
          taksit_durum: checkbox.getAttribute('taksitdurum') || '',
          checked_count: allCbs.filter(cb => cb.checked).length,
          source: 'lightblue'
        };
      }
      return {found:false, reason:'lightblue_satir_yok', checkbox_count: allCbs.length};
    }catch(e){
      return {found:false, reason:'js_hata:' + (e && e.message ? e.message : e)};
    }
  }
  const first = selectIn(window);
  if (first.found) return first;
  const iframes = Array.from(document.querySelectorAll('iframe'));
  for (const f of iframes){
    try{
      if (f.contentWindow){
        const info = selectIn(f.contentWindow);
        if (info.found) return info;
      }
    }catch(e){}
  }
  return first;
})()
"""
    info = konsola_js_oku(js_expr, bekle=1)
    if info.get("found"):
        logger.info(f"[ok] Lightblue checkbox secildi (zip eklenti mantigi): {info}")
    elif log_if_missing:
        logger.info(f"[info] Lightblue checkbox bulunamadi; mevcut secimle devam: {info}")
    return info or {}


def liste_risk_durumu() -> dict:
    js_expr = r"""
(function(){
  function norm(s){
    return (s||'').toLowerCase()
      .replace(/\s+/g,' ')
      .replace(/[\u00e7\u00c7]/g,'c').replace(/[\u011f\u011e]/g,'g').replace(/[\u0131\u0130]/g,'i')
      .replace(/[\u00f6\u00d6]/g,'o').replace(/[\u015f\u015e]/g,'s').replace(/[\u00fc\u00dc]/g,'u');
  }
  function isWarmRiskColor(bg){
    if (!bg) return false;
    const s = bg.toLowerCase();
    if (s.includes('yellow') || s.includes('orange') || s.includes('red')) return true;
    const m = s.match(/rgb\((\d+),\s*(\d+),\s*(\d+)\)/);
    if (!m) return false;
    const r = parseInt(m[1],10), g = parseInt(m[2],10), b = parseInt(m[3],10);
    return (r >= 200 && g >= 120 && b <= 170);
  }
  function scan(win){
    try{
      const cbs = Array.from(win.document.querySelectorAll('input[rel="TaksitCB"]'));
      for (const cb of cbs){
        const tr = cb.closest('tr');
        if (!tr) continue;
        const dur = (cb.getAttribute('taksitdurum') || '').toString().trim();
        const taksitNo = (cb.getAttribute('taksitno') || '').toString().trim();
        const cells = Array.from(tr.querySelectorAll(':scope > td, :scope > th'));
        const txt = norm((cells.length ? cells.map(x => x.textContent || '').join(' ') : tr.textContent || ''));
        const bg = (win.getComputedStyle ? win.getComputedStyle(tr).backgroundColor : '') || '';
        if (!isWarmRiskColor(bg)) continue;
        if (['2','3','6'].includes(dur) || txt.includes('kismi tahsilat') || txt.includes('takipte') || txt.includes('icrada')){
          return {risk:true, dur:dur, taksit_no:taksitNo, text:txt, bg:bg};
        }
      }
    }catch(e){}
    return {risk:false, dur:'', taksit_no:'', text:'', bg:''};
  }
  let r = scan(window);
  if (r.risk) return r;
  const iframes = Array.from(document.querySelectorAll('iframe'));
  for (const f of iframes){
    try{ if (f.contentWindow){ r = scan(f.contentWindow); if (r.risk) return r; } }catch(e){}
  }
  return {risk:false, dur:'', taksit_no:'', text:'', bg:''};
})()
"""
    return konsola_js_oku(js_expr, bekle=1)


def kismi_tahsilat_listede() -> dict:
    js_expr = r"""
(function(){
  function norm(s){
    return (s||'').toLowerCase()
      .replace(/\s+/g,' ')
      .replace(/[\u00e7\u00c7]/g,'c').replace(/[\u011f\u011e]/g,'g').replace(/[\u0131\u0130]/g,'i')
      .replace(/[\u00f6\u00d6]/g,'o').replace(/[\u015f\u015e]/g,'s').replace(/[\u00fc\u00dc]/g,'u');
  }
  function isYellowLike(bg){
    if (!bg) return false;
    const s = bg.toLowerCase();
    if (s.includes('yellow') || s.includes('orange')) return true;
    const m = s.match(/rgb\((\d+),\s*(\d+),\s*(\d+)\)/);
    if (!m) return false;
    const r = parseInt(m[1],10), g = parseInt(m[2],10), b = parseInt(m[3],10);
    return (r >= 200 && g >= 160 && b <= 170);
  }
  function rowInfo(win){
    try{
      const cbs = Array.from(win.document.querySelectorAll('input[rel="TaksitCB"]'));
      for (const cb of cbs){
        const tr = cb.closest('tr');
        if (!tr) continue;
        const dur = (cb.getAttribute('taksitdurum')||'').toString().trim();
        const taksitNo = (cb.getAttribute('taksitno')||'').toString().trim();
        const bg = (win.getComputedStyle ? win.getComputedStyle(tr).backgroundColor : '') || '';
        const txt = norm(tr.textContent || '');
        if (isYellowLike(bg) && txt.includes('kismi tahsilat')){
          return {found:true, dur:dur, taksit_no:taksitNo, bg:bg, text:txt};
        }
      }
    }catch(e){}
    return {found:false, dur:'', taksit_no:'', bg:'', text:''};
  }
  let r = rowInfo(window);
  if (r.found) return r;
  const iframes = Array.from(document.querySelectorAll('iframe'));
  for (const f of iframes){
    try{ if (f.contentWindow){ r = rowInfo(f.contentWindow); if (r.found) return r; } }catch(e){}
  }
  return {found:false, dur:'', taksit_no:'', bg:'', text:''};
})()
"""
    return konsola_js_oku(js_expr, bekle=1)


def tahsilata_git_detay() -> None:
    js = r'''
(function(){
  function clickIn(win){
    try{
      const imgs = Array.from(win.document.querySelectorAll('img'));
      for (const img of imgs){
        const t = ((img.alt || '') + ' ' + (img.title || '') + ' ' + (img.src || '')).toLowerCase();
        if (t.includes('tahsilat') || t.includes('para64x64.png')){
          const clickable = img.closest('a,button,div,span') || img;
          clickable.click();
          return true;
        }
      }
      const direct = win.document.querySelector('[onclick*="Tahsilat"]') || win.document.querySelector('[id*="Tahsilat"]');
      if (direct){ direct.click(); return true; }
    }catch(e){}
    return false;
  }
  if (clickIn(window)) return true;
  const iframes = Array.from(document.querySelectorAll('iframe'));
  for (const f of iframes){
    try{ if (f.contentWindow && clickIn(f.contentWindow)) return true; }catch(e){}
  }
  return false;
})()
'''
    konsola_js_yolla(js, bekle=2)
    # Fallback: coordinate click
    dx, dy = CONFIG["coords"].get("tahsilat_icon", (0, 0))
    if dx and dy:
        pyautogui.click(dx, dy)
    logger.info("[ok] Tahsilat butonu tiklandi (detay)")


def read_screen_values() -> dict:
    js_expr = r"""
(function(){
  function norm(s){
    return (s||'').toLowerCase()
      .replace(/\s+/g,' ')
      .replace(/[\u00e7\u00c7]/g,'c').replace(/[\u011f\u011e]/g,'g').replace(/[\u0131\u0130]/g,'i')
      .replace(/[\u00f6\u00d6]/g,'o').replace(/[\u015f\u015e]/g,'s').replace(/[\u00fc\u00dc]/g,'u');
  }
  function findAnaparaFromInputs(){
    function findInDoc(doc){
      try{
        const rows = Array.from(doc.querySelectorAll('tr'));
        for (const tr of rows){
          const label = norm(tr.textContent || '');
          if (label.includes('anapara')){
            const inp = tr.querySelector('input[rel="Borc"]') || tr.querySelector('input[type="text"]');
            if (inp && /[0-9]/.test(inp.value || '')) return inp.value;
          }
        }
      }catch(e){}
      return '';
    }
    let v = findInDoc(document);
    if (v) return v;
    const iframes = Array.from(document.querySelectorAll('iframe'));
    for (const f of iframes){
      try{
        if (f.contentWindow && f.contentWindow.document){
          v = findInDoc(f.contentWindow.document);
          if (v) return v;
        }
      }catch(e){}
    }
    return '';
  }
  function findAnaparaFromSelects(){
    function findInDoc(doc){
      try{
        const sels = Array.from(doc.querySelectorAll('select[rel="TutarTip"]'));
        for (const sel of sels){
          const opt = sel.options[sel.selectedIndex];
          const label = norm(opt ? opt.textContent : '');
          if (label.includes('anapara')){
            const tr = sel.closest('tr');
            const inp = tr ? (tr.querySelector('input[rel="Borc"]') || tr.querySelector('input[type="text"]')) : null;
            if (inp && /[0-9]/.test(inp.value || '')) return inp.value;
          }
        }
      }catch(e){}
      return '';
    }
    let v = findInDoc(document);
    if (v) return v;
    const iframes = Array.from(document.querySelectorAll('iframe'));
    for (const f of iframes){
      try{
        if (f.contentWindow && f.contentWindow.document){
          v = findInDoc(f.contentWindow.document);
          if (v) return v;
        }
      }catch(e){}
    }
    return '';
  }
  function findAnapara(){
    const tables = Array.from(document.querySelectorAll('table'));
    for (const tbl of tables){
      const t = norm(tbl.textContent || '');
      if (!t.includes('tutar tipleri')) continue;
      const rows = Array.from(tbl.querySelectorAll('tr'));
      for (const tr of rows){
        const cells = Array.from(tr.querySelectorAll('td,th'));
        if (!cells.length) continue;
        const label = norm(cells[0].textContent || '');
        if (label === 'anapara'){
          for (let i=1;i<cells.length;i++){
            const val = (cells[i].textContent || '').trim();
            if (/[0-9]/.test(val)) return val;
          }
        }
      }
    }
    return '';
  }
  function findById(id){
    let el = document.getElementById(id);
    if (!el) {
      const iframes = Array.from(document.querySelectorAll('iframe'));
      for (const f of iframes){
        try{
          if (f.contentWindow && f.contentWindow.document){
            el = f.contentWindow.document.getElementById(id);
            if (el) break;
          }
        }catch(e){}
      }
    }
    return el;
  }
  function collectBodyText(){
    let t = (document.body ? document.body.textContent : '') || '';
    const iframes = Array.from(document.querySelectorAll('iframe'));
    for (const f of iframes){
      try{
        if (f.contentWindow && f.contentWindow.document && f.contentWindow.document.body){
          t += ' ' + f.contentWindow.document.body.textContent;
        }
      }catch(e){}
    }
    return t;
  }
  function getCheckedRowInfo(){
    function fromWin(win){
      try{
        const cb = win.document.querySelector('input[rel="TaksitCB"]:checked');
        if (!cb) return {kismi:false, text:''};
        const tr = cb.closest('tr');
        const txt = norm(tr ? (tr.textContent || '') : '');
        return {
          kismi: txt.includes('kismi tahsilat'),
          text: txt
        };
      }catch(e){}
      return {kismi:false, text:''};
    }
    let r = fromWin(window);
    if (r.kismi || r.text) return r;
    const iframes = Array.from(document.querySelectorAll('iframe'));
    for (const f of iframes){
      try{
        if (f.contentWindow){
          r = fromWin(f.contentWindow);
          if (r.kismi || r.text) return r;
        }
      }catch(e){}
    }
    return {kismi:false, text:''};
  }
  function findTaksitNo(win){
    try{
      const el = win.document.getElementById('TahsilatEkranHareketAciklama');
      if (el && el.value){
        const m = el.value.match(/(\d+)\s*\./);
        if (m) return m[1];
      }
      const txt = (win.document.body ? win.document.body.textContent : '');
      const m2 = txt.match(/(\d+)\s*\.?\s*taksitin/i);
      if (m2) return m2[1];
    }catch(e){}
    return '';
  }
  function getTaksitNo(){
    let v = findTaksitNo(window);
    if (v) return v;
    const iframes = Array.from(document.querySelectorAll('iframe'));
    for (const f of iframes){
      try{
        if (f.contentWindow){
          v = findTaksitNo(f.contentWindow);
          if (v) return v;
        }
      }catch(e){}
    }
    return '';
  }
  const masrafInput = findById('TaksitTahsilatMasrafTutar');
  const gecikmeInput = findById('TaksitTahsilatGecikmeFaizi');
  const bankaKomisyonInput = findById('TaksitTahsilatDigerTutar');
  const checkedInfo = getCheckedRowInfo();
  function findTaksitDurum(win){
    try{
      const cb = win.document.querySelector('input[rel="TaksitCB"]:checked');
      if (cb) return cb.getAttribute('taksitdurum') || '';
    }catch(e){}
    return '';
  }
  function getTaksitDurum(){
    let v = findTaksitDurum(window);
    if (v) return v;
    const iframes = Array.from(document.querySelectorAll('iframe'));
    for (const f of iframes){
      try{ if (f.contentWindow){ v = findTaksitDurum(f.contentWindow); if (v) return v; } }catch(e){}
    }
    return '';
  }
    function findBorcTutar(win){
    try{
      const table = win.document.getElementById('TahsilatBorcAlacakTablo');
      const scope = table || win.document;
      const asilRows = Array.from(scope.querySelectorAll('tr[rel="Asil"]'));
      for (const tr of asilRows){
        const sel = tr.querySelector('select[rel="TutarTip"]');
        if (!sel) continue;
        const val = (sel.value || '').toString();
        const opt = sel.options[sel.selectedIndex];
        const label = norm(opt ? opt.textContent : '');
        if (val === '1' || label === 'anapara' || label.includes('anapara')){
          const inp = tr.querySelector('input[rel="Borc"]');
          if (inp && /[0-9]/.test(inp.value || '')) return inp.value;
        }
      }
      const inp = scope.querySelector('tr[rel="Asil"] input[rel="Borc"][value]');
      if (inp && /[0-9]/.test(inp.value || '')) return inp.value;
    }catch(e){}
    return '';
  }
  function getBorcTutar(){
    let v = findBorcTutar(window);
    if (v) return v;
    const iframes = Array.from(document.querySelectorAll('iframe'));
    for (const f of iframes){
      try{ if (f.contentWindow){ v = findBorcTutar(f.contentWindow); if (v) return v; } }catch(e){}
    }
    return '';
  }
return {
    anapara: (findAnaparaFromSelects() || findAnaparaFromInputs() || findAnapara()),
    masraf: masrafInput ? masrafInput.value : '',
    gecikme: gecikmeInput ? gecikmeInput.value : '',
    banka_komisyon: bankaKomisyonInput ? bankaKomisyonInput.value : '',
    kismi_tahsilat_var: !!checkedInfo.kismi,
    kismi_row_text: checkedInfo.text,
    taksit_no: getTaksitNo(),
    taksit_durum: getTaksitDurum(),
    borc_tutar: getBorcTutar()
  };
})()
"""
    return konsola_js_oku(js_expr, bekle=2)



def tahsilat_ekrani_doldur(tarih: str, dekont_no: str, tutar: str, banka_faizi: str, gecikme_faizi: str, masraf: str, banka_komisyon: str | None) -> None:
    js = """
function forceSetValue(el, val) {
  try {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    setter.call(el, val);
  } catch(e) {
    el.value = val;
  }
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
}
function findById(id){
  let el = document.getElementById(id);
  if (!el) {
    const iframes = Array.from(document.querySelectorAll('iframe'));
    for (const f of iframes) {
      try {
        if (f.contentWindow && f.contentWindow.document) {
          el = f.contentWindow.document.getElementById(id);
          if (el) break;
        }
      } catch (e) {}
    }
  }
  return el;
}
function setValue(id, val) {
  let el = findById(id);
  if (el) {
    try { el.readOnly = false; } catch(e) {}
    el.removeAttribute('readonly');
    forceSetValue(el, val);
  }
}
function selectDayWithDatepicker(id, dateStr) {
  let el = document.getElementById(id);
  if (!el) {
    const iframes = Array.from(document.querySelectorAll('iframe'));
    for (const f of iframes) {
      try {
        if (f.contentWindow && f.contentWindow.document) {
          el = f.contentWindow.document.getElementById(id);
          if (el) break;
        }
      } catch (e) {}
    }
  }
  if (!el) return;
  try { el.readOnly = false; } catch(e) {}
  el.removeAttribute('readonly');
  forceSetValue(el, dateStr);
  el.dispatchEvent(new Event('blur', { bubbles: true }));
}


selectDayWithDatepicker('TaksitTahsilatDekontTarihi', '__TARIH__');
setValue('TaksitTahsilatDekontNo', '__DEKONT_NO__');
setValue('TaksitTahsilatTutar', '__TUTAR__');
setValue('TaksitTahsilatBankaFaizi', '__BANKA_FAIZI__');
setValue('TaksitTahsilatGecikmeFaizi', '__GECIKME_FAIZI__');
setValue('TaksitTahsilatMasrafTutar', '__MASRAF__');
if (__BANKA_KOMISYON_YAZ__) {
  setValue('TaksitTahsilatDigerTutar', '__BANKA_KOMISYON__');
}
"""
    js = (js
          .replace('__TARIH__', tarih)
          .replace('__DEKONT_NO__', dekont_no)
          .replace('__TUTAR__', tutar)
          .replace('__BANKA_FAIZI__', banka_faizi)
          .replace('__GECIKME_FAIZI__', gecikme_faizi)
          .replace('__MASRAF__', masraf)
          .replace('__BANKA_KOMISYON_YAZ__', 'true' if banka_komisyon is not None else 'false')
          .replace('__BANKA_KOMISYON__', banka_komisyon or ''))
    konsola_js_yolla(js, bekle=2)

    # Islem yeri detay dropdown (tek secenek)
    sx, sy = CONFIG["coords"].get("islem_yeri_detay_open", (0, 0))
    ox, oy = CONFIG["coords"].get("islem_yeri_detay_option", (0, 0))
    if sx and sy:
        pyautogui.click(sx, sy)
        time.sleep(0.2)
        if ox and oy:
            pyautogui.click(ox, oy)
            time.sleep(0.1)
        else:
            pyautogui.hotkey('alt', 'down')
            time.sleep(0.1)
            pyautogui.press('enter')
        pyautogui.press('tab')
        time.sleep(0.1)

    # Koordinat ile dekont tarihi inputuna yapistir
    dx, dy = CONFIG["coords"].get("date_input", (0, 0))
    if dx and dy:
        pyautogui.click(dx, dy)
        time.sleep(0.2)
        pyautogui.hotkey('ctrl', 'a')
        pyperclip.copy(tarih)
        pyautogui.hotkey('ctrl', 'v')
        pyautogui.press('tab')

    # Dekont No inputuna koordinat ile yaz
    nx, ny = CONFIG["coords"].get("dekont_no_input", (0, 0))
    if nx and ny:
        pyautogui.click(nx, ny)
        time.sleep(0.2)
        pyperclip.copy(dekont_no)
        pyautogui.hotkey('ctrl', 'a')
        pyautogui.hotkey('ctrl', 'v')
        pyautogui.press('tab')

    logger.info(f"[ok] Tahsilat ekrani dolduruldu: {dekont_no}")


def js_wait_click_id(element_id: str, timeout_ms: int = 10000) -> None:
    """
    Try to click an element by id, searching main document and iframes.
    Waits up to timeout_ms.
    """
    js = f"""
(function(){{
  function tryClick(win, id){{
    try {{
      const el = win.document.getElementById(id);
      if (el) {{ el.click(); return true; }}
    }} catch(e) {{}}
    return false;
  }}
  function clickAny(id){{
    if (tryClick(window, id)) return true;
    const iframes = Array.from(document.querySelectorAll('iframe'));
    for (const f of iframes){{
      try {{
        if (f.contentWindow && tryClick(f.contentWindow, id)) return true;
      }} catch(e) {{}}
    }}
    return false;
  }}
  const t0 = Date.now();
  const timer = setInterval(() => {{
    try {{
      if (clickAny('{element_id}')) {{ clearInterval(timer); }}
    }} catch(e) {{}}
    if (Date.now() - t0 > {timeout_ms}) clearInterval(timer);
  }}, 200);
}})();
"""
    konsola_js_yolla(js, bekle=1)
    time.sleep(0.5)


def tahsilat_detay_olustur_onayla(anapara_raw: float, masraf_raw: float) -> str:
    js_wait_click_id('TaksitTahsilatTamamBt')
    time.sleep(1.0)
    js_wait_click_id('TaksitTahsilatIslemOnay')
    time.sleep(0.8)

    js_expr = r"""
(function(){
  function norm(s){
    return (s||'').toLowerCase()
      .replace(/\s+/g,' ')
      .replace(/[çÇ]/g,'c').replace(/[ğĞ]/g,'g').replace(/[ıİ]/g,'i')
      .replace(/[öÖ]/g,'o').replace(/[şŞ]/g,'s').replace(/[üÜ]/g,'u');
  }
  function isVisible(el){
    try{
      if (!el) return false;
      const st = el.ownerDocument.defaultView.getComputedStyle(el);
      return st.display !== 'none' && st.visibility !== 'hidden';
    }catch(e){}
    return !!el;
  }
  function findVals(win){
    try{
      const msgEl = win.document.getElementById('TahsilatIslemSonucAlan');
      const msg = msgEl ? (msgEl.textContent || '').trim() : '';
      const onayBtn = win.document.getElementById('TaksitTahsilatIslemOnay');
      const detayBtn = win.document.getElementById('TaksitTahsilatTamamBt');
      let anapara = '';
      let masraf = '';
      const rows = Array.from(win.document.querySelectorAll('tr'));
      for (const tr of rows){
        const sel = tr.querySelector('select[rel="TutarTip"]');
        if (!sel) continue;
        const opt = sel.options[sel.selectedIndex];
        const label = norm(opt ? opt.textContent : '');
        const inp = tr.querySelector('input[rel="Borc"]');
        if (!inp) continue;
        if (!anapara && label.includes('anapara')) anapara = inp.value || '';
        if (!masraf && label.includes('masraf')) masraf = inp.value || '';
      }
      return {
        msg: msg,
        anapara: anapara,
        masraf: masraf,
        onay_visible: isVisible(onayBtn),
        detay_visible: isVisible(detayBtn)
      };
    }catch(e){
      return {msg:'', anapara:'', masraf:'', onay_visible:false, detay_visible:false};
    }
  }
  let r = findVals(window);
  if (r.msg || r.anapara || r.masraf || r.onay_visible || r.detay_visible) return r;
  const iframes = Array.from(document.querySelectorAll('iframe'));
  for (const f of iframes){
    try{ if (f.contentWindow){ r = findVals(f.contentWindow); if (r.msg || r.anapara || r.masraf || r.onay_visible || r.detay_visible) return r; } }catch(e){}
  }
  return r;
})()
"""
    last_info = {}
    for _ in range(10):
        info = konsola_js_oku(js_expr, bekle=0.4)
        last_info = info or {}
        msg = str(last_info.get('msg') or '').strip()
        if msg:
            msg_n = normalize_text(msg).lower()
            if 'toplam borc ve toplam alacak esit olmali' in msg_n:
                a = parse_amount(last_info.get('anapara') or '0')
                m = parse_amount(last_info.get('masraf') or '0')
                logger.warning(f"[skip] Onay uyarisi: toplam borc ve alacak esit degil. Ekran anapara={a} masraf={m} Dekont anapara={anapara_raw} masraf={masraf_raw} Mesaj={msg}")
                return 'onay_uyarisi_esit_degil'
        if not last_info.get('onay_visible') and not last_info.get('detay_visible'):
            logger.info("[ok] Detay olustur + onayla tiklandi")
            return 'ok'
        time.sleep(0.3)

    logger.warning(f"[skip] Onay sonrasi pencere kapanmadi. Son durum: {last_info}")
    return 'onay_pencere_kapanmadi'

def sayfayi_en_uste_al() -> None:
    js = r'''
(function(){
  function topIt(win){
    try{
      win.scrollTo(0, 0);
      if (win.document && win.document.documentElement) win.document.documentElement.scrollTop = 0;
      if (win.document && win.document.body) win.document.body.scrollTop = 0;
      return true;
    }catch(e){}
    return false;
  }
  topIt(window);
  const iframes = Array.from(document.querySelectorAll('iframe'));
  for (const f of iframes){
    try{ if (f.contentWindow) topIt(f.contentWindow); }catch(e){}
  }
  return true;
})()
'''
    konsola_js_yolla(js, bekle=1)
    px, py = CONFIG['coords'].get('page_focus', (0, 0))
    if px and py:
        pyautogui.click(px, py)
        time.sleep(0.1)
        try:
            pyautogui.hotkey('ctrl', 'home')
            time.sleep(0.2)
            pyautogui.press('home')
            time.sleep(0.2)
            pyautogui.scroll(3000)
            time.sleep(0.3)
            pyautogui.scroll(3000)
            time.sleep(0.3)
        except Exception:
            pass

def kredi_listesi_acik_mi() -> bool:
    js_expr = r"""
(function(){
  function hasList(win){
    try{
      if (win.document.getElementById('OPKLKrediNo')) return true;
      const url = (win.location && win.location.href ? win.location.href : '').toLowerCase();
      if (url.includes('/tokbys/views/orjinalkredisec.aspx')) return true;
    }catch(e){}
    return false;
  }
  if (hasList(window)) return true;
  const iframes = Array.from(document.querySelectorAll('iframe'));
  for (const f of iframes){
    try{ if (f.contentWindow && hasList(f.contentWindow)) return true; }catch(e){}
  }
  return false;
})()
"""
    result = konsola_js_oku(js_expr, bekle=0.5)
    return bool(result)

def kredi_detay_acik_mi() -> bool:
    js_expr = r"""
(function(){
  function norm(s){
    return (s||'').toLowerCase()
      .replace(/\s+/g,' ')
      .replace(/[\u00e7\u00c7]/g,'c').replace(/[\u011f\u011e]/g,'g').replace(/[\u0131\u0130]/g,'i')
      .replace(/[\u00f6\u00d6]/g,'o').replace(/[\u015f\u015e]/g,'s').replace(/[\u00fc\u00dc]/g,'u');
  }
  function hasDetail(win){
    try{
      const url = (win.location && win.location.href ? win.location.href : '').toLowerCase();
      if (url.includes('/tokbys/views/kredi/ortakkredidetay.aspx')) return true;
      const body = norm(win.document.body ? win.document.body.textContent : '');
      if (body.includes('kredi turu') && body.includes('taksitler') && body.includes('hareketler')) return true;
    }catch(e){}
    return false;
  }
  if (hasDetail(window)) return true;
  const iframes = Array.from(document.querySelectorAll('iframe'));
  for (const f of iframes){
    try{ if (f.contentWindow && hasDetail(f.contentWindow)) return true; }catch(e){}
  }
  return false;
})()
"""
    result = konsola_js_oku(js_expr, bekle=0.5)
    return bool(result)

def baslangicta_kredi_listesine_git() -> None:
    kredi_listesine_don()


def kredi_listesine_don() -> None:
    js = r'''
(function(){
  function clickKrediListesi(win){
    try{
      const direct = win.document.querySelector('a.menuLink[href="/tokbys/Views/OrjinalKrediSec.aspx"]');
      if (direct){
        direct.click();
        return true;
      }
      const links = Array.from(win.document.querySelectorAll('a.menuLink'));
      for (const link of links){
        const href = (link.getAttribute('href') || '').toLowerCase();
        const text = (link.textContent || '').trim().toLowerCase();
        if (href.includes('/tokbys/views/orjinalkredisec.aspx') || text === 'kredi listesi'){
          link.click();
          return true;
        }
      }
    }catch(e){}
    return false;
  }
  if (clickKrediListesi(window)) return true;
  const iframes = Array.from(document.querySelectorAll('iframe'));
  for (const f of iframes){
    try{ if (f.contentWindow && clickKrediListesi(f.contentWindow)) return true; }catch(e){}
  }
  window.location.href = '/tokbys/Views/OrjinalKrediSec.aspx';
  return true;
})()
    '''
    konsola_js_yolla(js, bekle=2)

    for _ in range(10):
        if kredi_listesi_acik_mi():
            logger.info('[ok] Kredi listesine donuldu (menuLink JS)')
            time.sleep(2)
            return
        time.sleep(0.5)

    logger.warning('[warn] Kredi listesine donus dogrulanamadi')
    time.sleep(3)

def main() -> None:
    pdf_path = CONFIG["pdf_path"]
    if not Path(pdf_path).exists():
        logger.error(f"PDF bulunamadi: {pdf_path}")
        return

    rows = parse_dekonts(pdf_path)

    user_date = ""
    if CONFIG.get("prompt_dekont_tarihi"):
        user_date = input("Dekont tarihi giriniz (gg.aa.yyyy) - bos birakirsaniz PDF tarihi kullanilir: ").strip()
        if user_date:
            user_date = user_date.replace('/', '.')
            for r in rows:
                r["tarih"] = user_date

    effective_date = user_date or (rows[0].get("tarih") if rows else "") or time.strftime('%d.%m.%Y')
    output_dir = configure_output_paths(effective_date)
    logger.info(f"[ok] Cikti klasoru hazirlandi: {output_dir}")
    set_local_storage_dekont_date(effective_date)

    start_index = CONFIG["start_index"]
    progress = {}
    if CONFIG.get("resume_from_progress"):
        progress = load_progress(CONFIG["progress_path"])
        if progress.get("next_index") is not None and progress.get("next_index") > start_index:
            start_index = int(progress.get("next_index"))
            logger.info(f"[info] Kaldigi yerden devam: index={start_index} kredi={progress.get('last_kredi_no','')}")

    write_preview_csv(rows, CONFIG["preview_csv"])
    logger.info(f"[ok] Onizleme CSV yazildi: {CONFIG['preview_csv']}")

    if CONFIG["dry_run"]:
        logger.info("[info] dry_run=True, otomasyon calistirilmadi.")
        return

    konsolu_hazirla()
    baslangicta_kredi_listesine_git()

    for i, row in enumerate(rows):
        if i < start_index:
            continue
        if not row["kredi_no"]:
            logger.warning(f"[skip] Kredi no yok (sayfa {row['page']}).")
            save_progress(CONFIG["progress_path"], {"next_index": i+1, "last_kredi_no": "", "last_taksit_no": "", "status": "skip", "reason": "kredi_no_yok"})
            cx, cy = CONFIG["coords"].get("iframe_close", (0, 0))
            if cx and cy:
                pyautogui.click(cx, cy)
                time.sleep(0.5)
            continue

        while True:
            kredi_no_yaz(row.get("kredi_no_short") or row["kredi_no"])
            time.sleep(1)
            if krediye_git():
                break
            logger.error(f"[stop] Kredi detay ekrani acilamadi; yanlis tiklamayi onlemek icin durduruldu. Kredi: {row['kredi_no']}")
            save_progress(CONFIG["progress_path"], {
                "next_index": i,
                "last_kredi_no": row["kredi_no"],
                "last_taksit_no": "",
                "status": "error",
                "reason": "kredi_detayi_acilamadi",
            })
            if devam_iste("Kaldigin yerden devam edeyim mi?"):
                kredi_listesine_don()
                logger.info(f"[info] Ayni kredi tekrar deneniyor: {row['kredi_no']}")
                time.sleep(1)
                continue
            return
        time.sleep(1)
        taksitler_tabina_git()
        time.sleep(1)
        liste_risk = liste_risk_durumu()
        if liste_risk.get('risk'):
            risk_td = str(liste_risk.get('dur') or '').strip()
            risk_taksit_no = str(liste_risk.get('taksit_no') or '').strip()
            risk_text = str(liste_risk.get('text') or '').strip()
            if risk_td in {'2', '3', '6'}:
                reason = f'icra_takip_var_taksitdurum_{risk_td}'
            elif 'kismi tahsilat' in risk_text.lower():
                reason = 'kismi_tahsilat'
            elif 'takipte' in risk_text.lower():
                reason = 'takipte'
            elif 'icrada' in risk_text.lower():
                reason = 'icrada'
            else:
                reason = 'riskli_taksit_var'
            logger.warning(f"[skip] Liste riskli satir bulundu. Kredi: {row['kredi_no']} taksitdurum={risk_td} taksitno={risk_taksit_no} satir='{risk_text}'")
            append_result(CONFIG['result_csv'], {
                'kredi_no': row['kredi_no'],
                'taksit_no': risk_taksit_no,
                'taksit_durum': risk_td,
                'tarih': row.get('tarih', ''),
                'dekont_no': row.get('dekont_no', ''),
                'status': 'skip',
                'reason': reason,
            })
            save_progress(CONFIG['progress_path'], {'next_index': i+1, 'last_kredi_no': row['kredi_no'], 'last_taksit_no': risk_taksit_no, 'status': 'skip', 'reason': reason})
            kredi_listesine_don()
            continue

        if CONFIG["taksit_secim"] == "ilk_odenmedi":
            secim = taksit_sec()
            time.sleep(1)
            checked_count = int(secim.get("checked_count") or 0)
            secim_taksit_no = str(secim.get("taksit_no") or "")
            secim_durum = str(secim.get("taksit_durum") or "")
            if checked_count != 1 or not secim_taksit_no:
                reason = "tek_taksit_secilemedi"
                logger.warning(f"[skip] Tek taksit secimi basarisiz. Kredi: {row['kredi_no']} secim={secim}")
                append_result(CONFIG['result_csv'], {
                    'kredi_no': row['kredi_no'],
                    'taksit_no': secim_taksit_no,
                    'taksit_durum': secim_durum,
                    'tarih': row.get('tarih',''),
                    'dekont_no': row.get('dekont_no',''),
                    'status': 'skip',
                    'reason': reason,
                })
                save_progress(CONFIG['progress_path'], {
                    'next_index': i+1,
                    'last_kredi_no': row['kredi_no'],
                    'last_taksit_no': secim_taksit_no,
                    'status': 'skip',
                    'reason': reason
                })
                kredi_listesine_don()
                continue
        elif CONFIG["taksit_secim"] == "skip":
            time.sleep(1)

        tahsilata_git_detay()
        time.sleep(2)

        ekran = read_screen_values()
        ekran_anapara = parse_amount(ekran.get("anapara") or "0")
        ekran_masraf = parse_amount(ekran.get("masraf") or "0")
        ekran_gecikme = parse_amount(ekran.get("gecikme") or "0")
        ekran_banka_komisyon = parse_amount(ekran.get("banka_komisyon") or "0")
        kismi_var = bool(ekran.get("kismi_tahsilat_var"))
        taksit_no = str(ekran.get("taksit_no") or "")
        taksit_durum = str(ekran.get("taksit_durum") or "")

        if taksit_durum in {"2", "3", "6"}:
            reason = f"icra_takip_var_taksitdurum_{taksit_durum}"
            logger.warning(f"[skip] Icra/takip durumu var. Kredi: {row['kredi_no']} taksitdurum={taksit_durum} taksitno={taksit_no}")
            append_result(CONFIG['result_csv'], {
                'kredi_no': row['kredi_no'],
                'taksit_no': taksit_no,
                'taksit_durum': taksit_durum,
                'tarih': row.get('tarih', ''),
                'dekont_no': row.get('dekont_no', ''),
                'status': 'skip',
                'reason': reason,
            })
            save_progress(CONFIG['progress_path'], {'next_index': i+1, 'last_kredi_no': row['kredi_no'], 'last_taksit_no': taksit_no, 'status': 'skip', 'reason': reason})
            cx, cy = CONFIG['coords'].get('iframe_close', (0, 0))
            if cx and cy:
                pyautogui.click(cx, cy)
                time.sleep(0.5)
            kredi_listesine_don()
            continue

        if kismi_var:
            logger.warning(f"[skip] Durum kismi tahsilat. Kredi: {row['kredi_no']} sayfa {row['page']} Satir: {ekran.get('kismi_row_text','')}")
            append_result(CONFIG['result_csv'], {'kredi_no': row['kredi_no'], 'taksit_no': taksit_no, 'taksit_durum': taksit_durum, 'tarih': row.get('tarih',''), 'dekont_no': row.get('dekont_no',''), 'status': 'skip', 'reason': 'kismi_tahsilat'})
            save_progress(CONFIG['progress_path'], {'next_index': i+1, 'last_kredi_no': row['kredi_no'], 'last_taksit_no': taksit_no, 'status': 'skip', 'reason': 'kismi_tahsilat'})
            cx, cy = CONFIG['coords'].get('iframe_close', (0, 0))
            if cx and cy:
                pyautogui.click(cx, cy)
                time.sleep(0.5)
            kredi_listesine_don()
            continue

        if ekran_masraf > 0 and row['koop_masraf_raw'] == 0:
            logger.warning(f"[skip] Ekranda masraf var ama dekontta yok. Kredi: {row['kredi_no']} sayfa {row['page']}")
            append_result(CONFIG['result_csv'], {'kredi_no': row['kredi_no'], 'taksit_no': taksit_no, 'taksit_durum': taksit_durum, 'tarih': row.get('tarih',''), 'dekont_no': row.get('dekont_no',''), 'status': 'skip', 'reason': 'masraf_uyusmadi'})
            save_progress(CONFIG['progress_path'], {'next_index': i+1, 'last_kredi_no': row['kredi_no'], 'last_taksit_no': taksit_no, 'status': 'skip', 'reason': 'masraf_uyusmadi'})
            cx, cy = CONFIG['coords'].get('iframe_close', (0, 0))
            if cx and cy:
                pyautogui.click(cx, cy)
                time.sleep(0.5)
            kredi_listesine_don()
            continue

        if ekran_gecikme > 0 and parse_amount(row.get('gecikme_faizi') or '0') == 0:
            logger.info(f"[info] Ekranda gecikme faizi var, dekontta yok. 0,00 yapilacak. Kredi: {row['kredi_no']}")

        if ekran_anapara == 0:
            logger.warning(f"[warn] Ekran anapara okunamadi (0). Kredi: {row['kredi_no']} sayfa {row['page']}")

        tahsilat_tutari = row['tutar']
        yazilacak_banka_komisyon: str | None = row['banka_komisyon']
        if not row.get('banka_komisyon_dekontta_var') and ekran_banka_komisyon > 0:
            tahsilat_tutari = format_amount(
                parse_amount(row['tutar']) + ekran_banka_komisyon,
                CONFIG['use_decimal_comma'],
            )
            yazilacak_banka_komisyon = None
            logger.info(
                f"[info] Dekontta banka komisyonu yok; ekrandaki {ekran_banka_komisyon:.2f} TL korundu "
                f"ve tahsilat tutari {row['tutar']} -> {tahsilat_tutari} olarak yukseltildi."
            )

        tahsilat_ekrani_doldur(
            row['tarih'],
            row['dekont_no'],
            tahsilat_tutari,
            row['banka_faizi'],
            row['gecikme_faizi'],
            row['masraf'],
            yazilacak_banka_komisyon,
        )

        sonuc = tahsilat_detay_olustur_onayla(row['anapara_raw'], row['koop_masraf_raw'])
        if sonuc != 'ok':
            reason = sonuc or 'bilinmeyen_hata'
            logger.warning(f"[skip] Tahsilat onaylanmadi. Kredi: {row['kredi_no']} taksit={taksit_no} reason={reason}")
            append_result(CONFIG['result_csv'], {
                'kredi_no': row['kredi_no'],
                'taksit_no': taksit_no,
                'taksit_durum': taksit_durum,
                'tarih': row.get('tarih',''),
                'dekont_no': row.get('dekont_no',''),
                'status': 'skip',
                'reason': reason,
            })
            save_progress(CONFIG['progress_path'], {'next_index': i+1, 'last_kredi_no': row['kredi_no'], 'last_taksit_no': taksit_no, 'status': 'skip', 'reason': reason})
            cx, cy = CONFIG['coords'].get('iframe_close', (0, 0))
            if cx and cy:
                pyautogui.click(cx, cy)
                time.sleep(0.5)
            kredi_listesine_don()
            continue

        append_result(CONFIG['result_csv'], {
            'kredi_no': row['kredi_no'],
            'taksit_no': taksit_no,
            'taksit_durum': taksit_durum,
            'tarih': row.get('tarih', ''),
            'dekont_no': row.get('dekont_no', ''),
            'status': 'ok',
            'reason': '',
        })
        save_progress(CONFIG['progress_path'], {'next_index': i+1, 'last_kredi_no': row['kredi_no'], 'last_taksit_no': taksit_no, 'status': 'ok', 'reason': ''})
        time.sleep(1)
        kredi_listesine_don()


if __name__ == "__main__":
    main()
















import os
import sys
import json
import time
import shutil
import socket
import logging
import hashlib
import threading
import subprocess

import psutil
import requests
from tb_device_mqtt import TBDeviceMqttClient

# --- KONFIGURASI DIREKTORI ---
OTA_DIR = "/opt/ota-update"
os.makedirs(OTA_DIR, exist_ok=True)

LOG_FILE = os.path.join(OTA_DIR, "ota_agent.log")
STATE_FILE = os.path.join(OTA_DIR, "last_applied.json")

# --- KONFIGURASI THINGSBOARD ---
TB_HOST = "192.168.35.11"
TB_HTTP_PORT = 8080
TB_MQTT_PORT = 1883
TB_TOKEN = "ipbOEcOCwxzYAiiC9hyX"

METRIC_INTERVAL = 30   # detik, jeda kirim metrik
GRACE_DETIK = 10       # aplikasi harus tetap hidup selama ini setelah di-restart

# --- TARGET UPDATE ---
# kode  : awalan atribut/telemetry ThingsBoard (fw_*, sw_*)
# restart: "subprocess" = agent menjalankan aplikasinya sendiri
#          "systemd"    = agent menjalankan: systemctl restart <service>
#          "none"       = tidak ada restart otomatis
TARGETS = {
    "fw": {
        "nama": "firmware",
        "endpoint": "firmware",
        "app_file": os.path.join(OTA_DIR, "ec100_controller.py"),
        "restart": "subprocess",
        "service": "ec100-controller",
    },
    "sw": {
        "nama": "software",
        "endpoint": "software",
        "app_file": os.path.join(OTA_DIR, "ec100_software.py"),
        "restart": "subprocess",
        "service": "ec100-software",
    },
}

client = None
pending = {k: None for k in TARGETS}   # target terbaru dari ThingsBoard
state_lock = threading.Lock()
update_event = threading.Event()

# --- KONFIGURASI LOGGING ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------
# Utilitas umum
# ---------------------------------------------------------------
def kirim(data, jeda=1):
    """Kirim telemetry, lalu jeda singkat agar server sempat memproses."""
    if client:
        client.send_telemetry(data)
        if jeda:
            time.sleep(jeda)


def load_state():
    """Format: {"fw": {"title":..,"version":..}, "sw": {...}}.
    File lama (format tunggal) dianggap milik firmware."""
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
    except Exception:
        return {}
    if "title" in data and "version" in data:
        return {"fw": {"title": data["title"], "version": data["version"]}}
    return data


def save_state(kode, title, version):
    data = load_state()
    data[kode] = {"title": title, "version": version}
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, STATE_FILE)


def hitung_checksum(file_path, algo):
    nama = {"SHA256": "sha256", "SHA384": "sha384",
            "SHA512": "sha512", "MD5": "md5"}.get(algo)
    if not nama:
        return None
    h = hashlib.new(nama)
    with open(file_path, "rb") as f:
        for blok in iter(lambda: f.read(4096), b""):
            h.update(blok)
    return h.hexdigest()


def cek_sintaks(path):
    """Hanya untuk file .py: parse tanpa menjalankan."""
    with open(path, "r", encoding="utf-8") as f:
        compile(f.read(), path, "exec")


def get_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((TB_HOST, TB_MQTT_PORT))  # tidak mengirim paket, hanya memilih interface
        return s.getsockname()[0]
    except Exception:
        return "unknown"
    finally:
        s.close()


def kumpulkan_metrik():
    return {
        "uptime_s": int(time.time() - psutil.boot_time()),
        "cpu_percent": psutil.cpu_percent(interval=None),
        "mem_percent": psutil.virtual_memory().percent,
        "disk_percent": psutil.disk_usage("/").percent,
    }


def nonaktifkan_ota_bawaan_sdk(c):
    """Matikan jalur OTA bawaan SDK supaya tidak balapan dengan agent ini.
    Best effort: nama method bisa beda antar versi SDK."""
    dinonaktifkan = []
    for nama in ("__process_firmware", "__request_firmware_info", "__get_firmware",
                 "__process_software", "__request_software_info", "__get_software"):
        attr = "_TBDeviceMqttClient" + nama
        if hasattr(c, attr):
            setattr(c, attr, lambda *a, **k: None)
            dinonaktifkan.append(nama)
    if dinonaktifkan:
        logger.info(f"Jalur OTA bawaan SDK dinonaktifkan: {dinonaktifkan}")
    else:
        logger.warning("Method OTA bawaan SDK tidak ditemukan, jalur bawaan mungkin masih aktif.")


# ---------------------------------------------------------------
# Kontrol aplikasi (stop / start / cek hidup)
# ---------------------------------------------------------------
def _perintah_aplikasi(app_file):
    if app_file.endswith(".py"):
        return [sys.executable, app_file]
    return [app_file]


def _cari_proses(app_file):
    """Cari proses yang menjalankan app_file (python <file> atau eksekusi langsung)."""
    dasar = os.path.basename(app_file)
    hasil = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = p.info["cmdline"] or []
            if not cmd or p.info["pid"] == os.getpid():
                continue
            exe = os.path.basename(cmd[0])
            langsung = exe == dasar
            lewat_python = exe.startswith("python") and any(os.path.basename(a) == dasar for a in cmd[1:])
            if langsung or lewat_python:
                hasil.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return hasil


def _hentikan_aplikasi(app_file):
    prosesnya = _cari_proses(app_file)
    for p in prosesnya:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _, masih_hidup = psutil.wait_procs(prosesnya, timeout=5)
    for p in masih_hidup:
        try:
            p.kill()
        except psutil.Error:
            pass
    if prosesnya:
        logger.info(f"{len(prosesnya)} proses lama dihentikan: {os.path.basename(app_file)}")


def _ekor_log(path, n=300):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - n))
            return f.read().decode(errors="replace").strip()
    except Exception:
        return ""


def restart_aplikasi(kode):
    """Restart aplikasi untuk target ini, lalu pastikan ia tidak langsung mati.
    Return (berhasil, keterangan)."""
    cfg = TARGETS[kode]
    mode = cfg["restart"]
    app_file = cfg["app_file"]

    if mode == "none":
        return True, "restart dilewati"

    if mode == "systemd":
        svc = cfg["service"]
        r = subprocess.run(["systemctl", "restart", svc], capture_output=True)
        if r.returncode != 0:
            return False, f"systemctl restart {svc} gagal: {r.stderr.decode().strip()}"
        time.sleep(GRACE_DETIK)
        r = subprocess.run(["systemctl", "is-active", "--quiet", svc])
        if r.returncode != 0:
            return False, f"service {svc} tidak aktif setelah restart"
        return True, f"service {svc} aktif"

    # mode "subprocess"
    _hentikan_aplikasi(app_file)
    log_app = os.path.join(OTA_DIR, f"app_{kode}.log")
    with open(log_app, "ab") as lf:
        proc = subprocess.Popen(_perintah_aplikasi(app_file), cwd=OTA_DIR,
                                stdout=lf, stderr=subprocess.STDOUT,
                                start_new_session=True)
    batas = time.time() + GRACE_DETIK
    while time.time() < batas:
        kode_keluar = proc.poll()
        if kode_keluar is not None:
            if kode_keluar == 0:
                return True, "aplikasi selesai dengan kode 0"
            ekor = _ekor_log(log_app)
            return False, f"aplikasi berhenti dengan kode {kode_keluar}. {ekor}"
        time.sleep(0.5)
    return True, f"aplikasi berjalan (pid {proc.pid})"


# ---------------------------------------------------------------
# Proses update (dijalankan dari loop utama, bukan dari callback MQTT)
# ---------------------------------------------------------------
def apply_update(kode, pkg):
    cfg = TARGETS[kode]
    nama = cfg["nama"]
    app_file = cfg["app_file"]
    backup_file = app_file + ".bak"
    new_file = app_file + ".new"

    title = pkg["title"]
    version = pkg["version"]
    checksum = pkg.get("checksum")
    algo = (pkg.get("algo") or "SHA256").upper()

    k_state, k_error = f"{kode}_state", f"{kode}_error"
    k_cur_title, k_cur_ver = f"current_{kode}_title", f"current_{kode}_version"

    # Lewati kalau versi ini sudah terpasang, tapi sinkronkan status di dashboard
    terakhir = load_state().get(kode, {})
    if terakhir.get("title") == title and terakhir.get("version") == version:
        logger.info(f"[{nama}] {title} ({version}) sudah terpasang, update dilewati.")
        kirim({k_state: "UPDATED", k_cur_title: title,
               k_cur_ver: version, k_error: ""}, jeda=0)
        return

    logger.info(f"[{nama}] OTA Update Triggered: {title} ({version})")
    sudah_diganti = False
    ada_sebelumnya = os.path.exists(app_file)

    try:
        kirim({k_state: "DOWNLOADING"})

        url = f"http://{TB_HOST}:{TB_HTTP_PORT}/api/v1/{TB_TOKEN}/{cfg['endpoint']}"
        logger.info(f"[{nama}] Mengunduh {title} ({version}) dari ThingsBoard {TB_HOST}:{TB_HTTP_PORT}")

        resp = requests.get(url, params={"title": title, "version": version},
                            stream=True, timeout=60)
        resp.raise_for_status()

        with open(new_file, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        kirim({k_state: "DOWNLOADED"})

        if checksum:
            lokal = hitung_checksum(new_file, algo)
            if lokal is None:
                logger.warning(f"[{nama}] Algoritma checksum {algo} tidak didukung, verifikasi dilewati.")
            elif lokal.lower() != checksum.lower():
                raise ValueError(f"Checksum mismatch! Lokal: {lokal}, TB: {checksum}")
            else:
                kirim({k_state: "VERIFIED"})

        if app_file.endswith(".py"):
            try:
                cek_sintaks(new_file)
            except SyntaxError as e:
                raise RuntimeError(f"Syntax error pada update: {e}")

        kirim({k_state: "UPDATING"})

        if ada_sebelumnya:
            logger.info(f"[{nama}] Membuat backup versi yang sedang berjalan...")
            shutil.copy2(app_file, backup_file)

        shutil.move(new_file, app_file)
        sudah_diganti = True
        os.chmod(app_file, 0o755)

        logger.info(f"[{nama}] Me-restart aplikasi...")
        ok, ket = restart_aplikasi(kode)
        if not ok:
            raise RuntimeError(f"Aplikasi gagal jalan setelah update: {ket}")
        logger.info(f"[{nama}] {ket}")

        logger.info(f"[{nama}] Update sukses!")
        if os.path.exists(backup_file):
            os.remove(backup_file)

        save_state(kode, title, version)
        kirim({k_state: "UPDATED", k_cur_title: title,
               k_cur_ver: version, k_error: ""})

    except Exception as e:
        error_msg = str(e)
        logger.error(f"[{nama}] OTA Gagal: {error_msg}")

        if os.path.exists(new_file):
            os.remove(new_file)

        if sudah_diganti:
            logger.error(f"[{nama}] Memulai proses ROLLBACK...")
            if os.path.exists(backup_file):
                shutil.move(backup_file, app_file)
                logger.info(f"[{nama}] File dikembalikan ke versi sebelumnya.")
                ok, ket = restart_aplikasi(kode)
                logger.info(f"[{nama}] Restart versi lama: {ket}" if ok
                            else f"[{nama}] Versi lama pun gagal jalan: {ket}")
            elif not ada_sebelumnya:
                os.remove(app_file)
                logger.info(f"[{nama}] Instalasi pertama dibatalkan, file dihapus.")
            else:
                logger.critical(f"[{nama}] File backup tidak ditemukan.")

        kirim({k_state: "FAILED", k_error: error_msg[:250]})


# ---------------------------------------------------------------
# Callback MQTT: hanya mencatat target, tidak memproses apa pun
# ---------------------------------------------------------------
def on_attributes_change(result, exception=None):
    if exception is not None:
        logger.error(f"Gagal menerima atribut: {exception}")
        return

    attributes = result.get("shared", result)
    if not isinstance(attributes, dict):
        return

    ada = False
    with state_lock:
        for kode in TARGETS:
            title = attributes.get(f"{kode}_title")
            version = attributes.get(f"{kode}_version")
            if title is None or version is None:
                continue
            pending[kode] = {
                "title": title,
                "version": version,
                "checksum": attributes.get(f"{kode}_checksum"),
                "algo": attributes.get(f"{kode}_checksum_algorithm"),
            }
            ada = True
    if ada:
        update_event.set()


# ---------------------------------------------------------------
# Main
# ---------------------------------------------------------------
def main():
    global client
    client = TBDeviceMqttClient(TB_HOST, port=TB_MQTT_PORT, username=TB_TOKEN)
    nonaktifkan_ota_bawaan_sdk(client)

    try:
        client.connect()
        logger.info(f"Agent OTA terhubung ke ThingsBoard {TB_HOST} (MQTT: {TB_MQTT_PORT})")

        client.subscribe_to_all_attributes(on_attributes_change)
        shared_keys = [f"{k}_{s}" for k in TARGETS
                       for s in ("title", "version", "checksum", "checksum_algorithm")]
        client.request_attributes(shared_keys=shared_keys, callback=on_attributes_change)

        # Sinkronkan versi terpasang ke dashboard saat agent start
        sinkron = {}
        for kode, info in load_state().items():
            if kode in TARGETS:
                sinkron[f"current_{kode}_title"] = info.get("title")
                sinkron[f"current_{kode}_version"] = info.get("version")
        if sinkron:
            client.send_telemetry(sinkron)

        psutil.cpu_percent(interval=None)  # panggilan pertama selalu 0, jadi "dibuang" dulu
        last_metric = 0
        last_ip = None

        while True:
            if update_event.wait(timeout=1):
                update_event.clear()
                with state_lock:
                    tugas = [(k, pending[k]) for k in TARGETS if pending[k]]
                    for k, _ in tugas:
                        pending[k] = None
                for kode, pkg in tugas:
                    try:
                        apply_update(kode, pkg)
                    except Exception as e:
                        logger.exception(f"Kesalahan tak terduga saat update {kode}: {e}")

            now = time.time()
            if now - last_metric >= METRIC_INTERVAL:
                try:
                    client.send_telemetry(kumpulkan_metrik())
                    ip = get_ip()
                    if ip != last_ip:
                        client.send_attributes({"ip_address": ip})
                        last_ip = ip
                except Exception as e:
                    logger.warning(f"Gagal kirim metrik: {e}")
                last_metric = now

    except KeyboardInterrupt:
        logger.info("Mematikan agent...")
    finally:
        client.disconnect()


if __name__ == "__main__":
    main()

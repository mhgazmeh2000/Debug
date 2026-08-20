"""
پیاده‌سازی دستی SNMPv1 و v2c بدون وابستگی خارجی:
- encode_oid / encode_length
- build_snmp_get_v1, build_snmp_get_v2c
- parse_snmp_response
- کش نسخه SNMP برای هر IP (تشخیص خودکار SNMPv1/v2c)
- snmp_get (نسخه‌ای)
- snmp_get_with_fallback (با کش و تست هوشمند)
- snmp_get_bulk
"""

import socket
import threading
import logging
import time

log = logging.getLogger("PrinterMonitor")

SNMP_ERROR_NAMES = {
    0: "noError", 1: "tooBig", 2: "noSuchName", 3: "badValue",
    4: "readOnly", 5: "genErr", 6: "noAccess", 7: "wrongType",
    8: "wrongLength", 9: "wrongEncoding", 10: "wrongValue", 11: "noCreation",
    12: "inconsistentValue", 13: "resourceUnavailable", 14: "commitFailed",
    15: "undoFailed", 16: "authorizationError", 17: "notWritable", 18: "inconsistentName"
}

# ─── کش نسخه SNMP برای هر IP/community ────────────────────────────
# هر ورودی به شکل زیر ذخیره می‌شود:
#   {"version": 1|2|None, "expires_at": monotonic_ts|None}
# برای نسخه‌های ناموفق (offline / timeout) negative-cache با TTL کوتاه نگه می‌داریم
# تا در هر pull دوباره probe نشود.
_SNMP_VERSION_CACHE = {}
_version_cache_lock = threading.Lock()
_NEGATIVE_CACHE_TTL = 30.0
_POSITIVE_CACHE_TTL = 3600.0


def _version_cache_key(ip: str, community: str) -> str:
    return f"{ip}|{community}"



def _get_cached_version(ip: str, community: str):
    key = _version_cache_key(ip, community)
    with _version_cache_lock:
        entry = _SNMP_VERSION_CACHE.get(key)
        if entry is None and ip in _SNMP_VERSION_CACHE:
            # backward compatibility با کش‌های قدیمی فقط بر پایه IP
            legacy = _SNMP_VERSION_CACHE.get(ip)
            if legacy in (1, 2, None):
                entry = {"version": legacy, "expires_at": None if legacy in (1, 2) else time.monotonic() + _NEGATIVE_CACHE_TTL}
                _SNMP_VERSION_CACHE[key] = entry
                _SNMP_VERSION_CACHE.pop(ip, None)
        if not isinstance(entry, dict):
            return False, None
        expires_at = entry.get("expires_at")
        if expires_at is not None and time.monotonic() >= expires_at:
            _SNMP_VERSION_CACHE.pop(key, None)
            return False, None
        return True, entry.get("version")



def _set_cached_version(ip: str, community: str, version: int | None, ttl: float | None):
    key = _version_cache_key(ip, community)
    with _version_cache_lock:
        _SNMP_VERSION_CACHE[key] = {
            "version": version,
            "expires_at": (time.monotonic() + ttl) if ttl is not None else None,
        }



def _clear_cached_version(ip: str, community: str):
    key = _version_cache_key(ip, community)
    with _version_cache_lock:
        _SNMP_VERSION_CACHE.pop(key, None)
        _SNMP_VERSION_CACHE.pop(ip, None)



def _detect_snmp_version(ip: str, community: str, port: int = 161, probe_timeout: float = 1.5) -> int | None:
    """
    تشخیص نسخه SNMP با چند OID متداول و vendor-specific.

    بعضی دستگاه‌ها به sysUpTime یا sysDescr استاندارد پاسخ نمی‌دهند ولی به
    sysName، Printer-MIB model یا OIDهای اختصاصی vendor پاسخ معتبر می‌دهند.
    بنابراین برای backward compatibility و پشتیبانی از مدل‌های ناسازگار،
    چند OID را به‌ترتیب امتحان می‌کنیم.
    """
    found, cached_version = _get_cached_version(ip, community)
    if found:
        return cached_version

    probe_oids = [
        "1.3.6.1.2.1.1.3.0",                      # sysUpTime
        "1.3.6.1.2.1.1.1.0",                      # sysDescr
        "1.3.6.1.2.1.1.5.0",                      # sysName
        "1.3.6.1.2.1.43.5.1.1.16.1",              # Printer-MIB model
        "1.3.6.1.4.1.1129.2.3.50.1.2.3.1.3.1.1",  # Toshiba model
        "1.3.6.1.4.1.47206.1.0",                    # ECS100G model
        "1.3.6.1.4.1.47206.110.1.2.0",              # ECS100G temp1
    ]

    # ✅ فیکس offline کاذب: دو گذر می‌کنیم. گذر اول با timeout عادی؛ اگر هیچ
    # پاسخی نیامد، گذر دوم با timeout بزرگ‌تر روی OIDهای کلیدی — برخی مدل‌های
    # قدیمی Toshiba (مثل e-STUDIO 2505/257/3540 و بردهای اترنت قدیمی) به‌خصوص
    # بعد از idle، به اولین درخواست‌‌ها کند (۲-۴ ثانیه) پاسخ می‌دهند و گذر اول
    # را از دست می‌دهند؛ همان تک‌خط تایم‌اوت قبلاً باعث offline کاذب می‌شد.
    slow_probe_timeout = max(3.5, float(probe_timeout) * 2.3)
    for attempt_timeout, oid_subset in (
        (probe_timeout, probe_oids),
        (slow_probe_timeout, probe_oids[:3] + [probe_oids[4]]),
    ):
        for oid in oid_subset:
            # 🔥 تغییر قبلی: اول v1 تست می‌شود (چون در شبکه‌های واقعی رایج‌تر است)
            res_v1 = snmp_get(ip, oid, community, port, attempt_timeout, version=1)
            if res_v1 is not None:
                _set_cached_version(ip, community, 1, _POSITIVE_CACHE_TTL)
                log.debug(f"SNMP cache: {ip} -> v1 using {oid} (timeout={attempt_timeout}s)")
                return 1

            # سپس v2c
            res_v2 = snmp_get(ip, oid, community, port, attempt_timeout, version=2)
            if res_v2 is not None:
                _set_cached_version(ip, community, 2, _POSITIVE_CACHE_TTL)
                log.info(f"SNMP cache: {ip} -> v2c using {oid} (v1 failed, timeout={attempt_timeout}s)")
                return 2

    # هیچ‌یک پاسخ ندادند؛ احتمالاً دستگاه آفلاین یا SNMP مسدود است.
    log.debug(f"SNMP cache: {ip} unreachable (all probe OIDs failed in 2 passes, cached for {_NEGATIVE_CACHE_TTL:.0f}s)")
    _set_cached_version(ip, community, None, _NEGATIVE_CACHE_TTL)
    return None


# ─── توابع کدگذاری/کدگشایی SNMP (بدون تغییر) ──────────────────────────
def encode_oid(oid_str: str) -> bytes:
    parts = [int(x) for x in oid_str.strip(".").split(".")]
    encoded = [40 * parts[0] + parts[1]]
    for part in parts[2:]:
        if part == 0:
            encoded.append(0)
        else:
            subids = []
            while part > 0:
                subids.append(part & 0x7F)
                part >>= 7
            subids.reverse()
            for i, s in enumerate(subids):
                encoded.append(s | 0x80 if i < len(subids) - 1 else s)
    return bytes(encoded)


def encode_length(n: int) -> bytes:
    if n < 128:
        return bytes([n])
    elif n < 256:
        return bytes([0x81, n])
    else:
        return bytes([0x82, (n >> 8) & 0xFF, n & 0xFF])


def build_snmp_get_v1(community: str, oid: str, request_id: int = 1) -> bytes:
    ob = encode_oid(oid)
    oid_tlv = b'\x06' + encode_length(len(ob)) + ob
    vb = b'\x30' + encode_length(len(oid_tlv) + 2) + oid_tlv + b'\x05\x00'
    vbl = b'\x30' + encode_length(len(vb)) + vb
    rid = request_id & 0x7FFFFFFF
    rb = rid.to_bytes((rid.bit_length() + 8) // 8, 'big')
    rid_tlv = b'\x02' + encode_length(len(rb)) + rb
    pdu_body = rid_tlv + b'\x02\x01\x00\x02\x01\x00' + vbl
    pdu = b'\xa0' + encode_length(len(pdu_body)) + pdu_body
    cb = community.encode()
    comm_tlv = b'\x04' + encode_length(len(cb)) + cb
    msg_body = b'\x02\x01\x00' + comm_tlv + pdu
    return b'\x30' + encode_length(len(msg_body)) + msg_body


def build_snmp_get_v2c(community: str, oid: str, request_id: int = 1) -> bytes:
    ob = encode_oid(oid)
    oid_tlv = b'\x06' + encode_length(len(ob)) + ob
    vb = b'\x30' + encode_length(len(oid_tlv) + 2) + oid_tlv + b'\x05\x00'
    vbl = b'\x30' + encode_length(len(vb)) + vb
    rid = request_id & 0x7FFFFFFF
    rb = rid.to_bytes((rid.bit_length() + 8) // 8, 'big')
    rid_tlv = b'\x02' + encode_length(len(rb)) + rb
    pdu_body = rid_tlv + b'\x02\x01\x00\x02\x01\x00' + vbl
    pdu = b'\xa0' + encode_length(len(pdu_body)) + pdu_body
    cb = community.encode()
    comm_tlv = b'\x04' + encode_length(len(cb)) + cb
    msg_body = b'\x02\x01\x01' + comm_tlv + pdu
    return b'\x30' + encode_length(len(msg_body)) + msg_body


def parse_snmp_response_debug(data: bytes, expected_request_id=None):
    """نسخه‌ی تشخیصی parser که نوع پاسخ SNMP را با جزئیات برمی‌گرداند."""
    try:
        pos = 0

        def rl(d, p):
            if d[p] & 0x80 == 0:
                return d[p], p + 1
            n = d[p] & 0x7F
            return int.from_bytes(d[p + 1:p + 1 + n], 'big'), p + 1 + n

        if data[pos] != 0x30:
            return {"status": "parse_error", "reason": "invalid_sequence"}
        pos += 1
        _, pos = rl(data, pos)

        if data[pos] != 0x02:
            return {"status": "parse_error", "reason": "missing_version"}
        pos += 1
        version_len, pos = rl(data, pos)
        version_int = int.from_bytes(data[pos:pos + version_len], 'big')
        pos += version_len

        if data[pos] != 0x04:
            return {"status": "parse_error", "reason": "missing_community"}
        pos += 1
        comm_len, pos = rl(data, pos)
        community = data[pos:pos + comm_len].decode('utf-8', errors='ignore')
        pos += comm_len

        pdu_type = data[pos]
        if pdu_type != 0xa2:
            return {"status": "parse_error", "reason": f"unexpected_pdu_type_{hex(pdu_type)}"}
        pos += 1
        _, pos = rl(data, pos)

        if data[pos] != 0x02:
            return {"status": "parse_error", "reason": "missing_request_id"}
        pos += 1
        rid_len, pos = rl(data, pos)
        rid = int.from_bytes(data[pos:pos + rid_len], 'big')
        pos += rid_len
        if expected_request_id is not None and rid != expected_request_id:
            return {
                "status": "request_id_mismatch",
                "request_id": rid,
                "expected_request_id": expected_request_id,
                "version": version_int,
                "community": community,
            }

        if data[pos] != 0x02:
            return {"status": "parse_error", "reason": "missing_error_status"}
        pos += 1
        err_len, pos = rl(data, pos)
        error_status = int.from_bytes(data[pos:pos + err_len], 'big')
        pos += err_len

        if data[pos] != 0x02:
            return {"status": "parse_error", "reason": "missing_error_index"}
        pos += 1
        idx_len, pos = rl(data, pos)
        error_index = int.from_bytes(data[pos:pos + idx_len], 'big') if idx_len else 0
        pos += idx_len

        if error_status != 0:
            return {
                "status": "error_status",
                "error_status": error_status,
                "error_name": SNMP_ERROR_NAMES.get(error_status, f"err#{error_status}"),
                "error_index": error_index,
                "request_id": rid,
                "version": version_int,
                "community": community,
            }

        if data[pos] != 0x30:
            return {"status": "parse_error", "reason": "missing_varbind_list"}
        pos += 1
        _, pos = rl(data, pos)

        if data[pos] != 0x30:
            return {"status": "parse_error", "reason": "missing_varbind"}
        pos += 1
        _, pos = rl(data, pos)

        if data[pos] != 0x06:
            return {"status": "parse_error", "reason": "missing_oid"}
        pos += 1
        oid_len, pos = rl(data, pos)
        oid_bytes = data[pos:pos + oid_len]
        pos += oid_len

        vt = data[pos]
        pos += 1
        vl, pos = rl(data, pos)
        vb = data[pos:pos + vl]

        result = {
            "status": "ok",
            "request_id": rid,
            "version": version_int,
            "community": community,
            "value_tag": hex(vt),
            "raw_value_hex": vb.hex(),
            "raw_oid_hex": oid_bytes.hex(),
        }

        if vt == 0x02:
            result["value"] = 0 if vl == 0 else int.from_bytes(vb, 'big', signed=True)
            result["value_type"] = "integer"
            return result
        elif vt == 0x04:
            try:
                result["value"] = vb.decode('utf-8').strip('\x00').strip()
            except Exception:
                result["value"] = vb.hex()
            result["value_type"] = "string"
            return result
        elif vt in (0x41, 0x42, 0x43, 0x44):
            result["value"] = int.from_bytes(vb, 'big')
            result["value_type"] = "unsigned"
            return result
        elif vt == 0x40:
            result["value"] = ".".join(str(b) for b in vb)
            result["value_type"] = "ipaddress"
            return result
        elif vt == 0x80:
            return {**result, "status": "no_such_object", "value": None, "value_type": "exception"}
        elif vt == 0x81:
            return {**result, "status": "no_such_instance", "value": None, "value_type": "exception"}
        elif vt == 0x82:
            return {**result, "status": "end_of_mib_view", "value": None, "value_type": "exception"}
        return {**result, "status": "unsupported_value_type", "value": None}
    except Exception as e:
        log.debug(f"SNMP parse error: {e}")
        return {"status": "parse_error", "reason": str(e)}


def parse_snmp_response(data: bytes, expected_request_id=None):
    parsed = parse_snmp_response_debug(data, expected_request_id=expected_request_id)
    return parsed.get("value") if parsed.get("status") == "ok" else None


def snmp_get(ip: str, oid: str, community: str = "public",
             port: int = 161, timeout: float = 3.0, request_id: int = 1, version: int = 2):
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        if version == 1:
            pkt = build_snmp_get_v1(community, oid, request_id)
        else:
            pkt = build_snmp_get_v2c(community, oid, request_id)
        s.sendto(pkt, (ip, port))
        resp, _ = s.recvfrom(65535)
        parsed = parse_snmp_response_debug(resp, expected_request_id=request_id)
        # ✅ فیکس سازگاری: برخی پرینت‌سرورها/فیریمورهای قدیمی (مدل‌های قدیمی
        # Toshiba، JetDirect قدیمی و برخی cloneها) request-id اشتباه برمی‌گردانند.
        # چون سوکت UDP مخصوص همین درخواست است (پورت ephemeral)، پاسخِ دریافتی
        # عملاً مربوط به همین درخواست است — اگر فقط RID ناسازگار بود، مقدار را قبول کن.
        if parsed.get("status") == "request_id_mismatch":
            log.debug(
                "snmp_get %s %s v%s: accepting response with mismatched request id (%s→%s)",
                ip, oid, version, parsed.get("request_id"), request_id,
            )
            parsed = parse_snmp_response_debug(resp, expected_request_id=None)
        return parsed.get("value") if parsed.get("status") == "ok" else None
    except socket.timeout:
        return None
    except Exception as e:
        log.debug(f"snmp_get {ip} {oid} version {version}: {e}")
        return None
    finally:
        if s:
            s.close()



def snmp_debug_get(ip: str, oid: str, community: str = "public",
                   port: int = 161, timeout: float = 3.0, request_id: int = 1, version: int = 2):
    """درخواست تشخیصی SNMP که جزئیات نوع پاسخ را برمی‌گرداند."""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        if version == 1:
            pkt = build_snmp_get_v1(community, oid, request_id)
        else:
            pkt = build_snmp_get_v2c(community, oid, request_id)
        s.sendto(pkt, (ip, port))
        resp, _ = s.recvfrom(65535)
        parsed = parse_snmp_response_debug(resp, expected_request_id=request_id)
        parsed.update({
            "oid": oid,
            "ip": ip,
            "community": community,
            "version_requested": version,
        })
        return parsed
    except socket.timeout:
        return {
            "status": "timeout",
            "oid": oid,
            "ip": ip,
            "community": community,
            "version_requested": version,
        }
    except Exception as e:
        log.debug(f"snmp_debug_get {ip} {oid} version {version}: {e}")
        return {
            "status": "exception",
            "oid": oid,
            "ip": ip,
            "community": community,
            "version_requested": version,
            "error": str(e),
        }
    finally:
        if s:
            s.close()


def snmp_get_with_fallback(ip: str, oid: str, community: str = "public",
                           port: int = 161, timeout: float = 3.0, request_id: int = 1,
                           version: int | None = None):
    """
    دریافت SNMP با تشخیص خودکار و کش نسخه.
    ابتدا نسخه مناسب (1 یا 2) را بر اساس تست قبلی یا تست جدید تعیین می‌کند.
    """
    if version in (1, 2):
        return snmp_get(ip, oid, community, port, timeout, request_id, version=version)

    # کش را چک می‌کنیم؛ اگر موجود نبود، تشخیص دهیم (با timeout کوتاه)
    version = _detect_snmp_version(ip, community, port, probe_timeout=1.5)
    # اگر نسخه شناسایی نشد (None) → دستگاه به نظر آفلاین است
    if version is None:
        return None

    # درخواست با نسخه ترجیحی
    result = snmp_get(ip, oid, community, port, timeout, request_id, version=version)
    if result is not None:
        return result

    # اگر نسخه ترجیحی جواب نداد (مثلاً دستگاه ریبوت شده و v1/v2 تغییر کرده)،
    # نسخه دیگر را امتحان کن
    other_version = 2 if version == 1 else 1
    result_other = snmp_get(ip, oid, community, port, timeout, request_id, version=other_version)
    if result_other is not None:
        _set_cached_version(ip, community, other_version, _POSITIVE_CACHE_TTL)
        log.info(f"SNMP cache updated: {ip} -> v{other_version} (v{version} failed)")
        return result_other

    # ✅ فیکس مهم: شکست خواندن «این OID به‌خصوص» نباید کش نسخه را پاک یا negative
    # کند! قبلاً یک OID کند/محافظت‌شده (که پاسخش بی‌صدا drop می‌شد — رفتار رایج
    # firmwareهای قدیمی Toshiba) باعث می‌شد کل دستگاه برای چرخه‌های بعدی آفلاین
    # اعلام شود، در حالی که OIDهای دیگر پاسخ می‌دادند. کش نسخه فقط توسط
    # _detect_snmp_version (probe کامل) مدیریت می‌شود.
    log.debug(
        "snmp_get_with_fallback: OID %s on %s unreadable via v%s and v%s; version cache kept (v%s)",
        oid, ip, version, other_version, version,
    )
    return None


def snmp_get_first(ip: str, oids: list[str], community: str = "public",
                   port: int = 161, timeout: float = 3.0, version: int | None = None):
    """اولین OID پاسخ‌گو را از یک لیست برمی‌گرداند.

    خروجی:
      (value, oid)
    اگر هیچ OID پاسخ ندهد:
      (None, None)
    """
    for idx, oid in enumerate(oids, 1):
        value = snmp_get_with_fallback(
            ip,
            oid,
            community,
            port=port,
            timeout=timeout,
            request_id=idx,
            version=version,
        )
        if value is not None:
            return value, oid
    return None, None


def snmp_get_bulk(ip: str, oids: dict, community: str = "public") -> dict:
    """چند OID را یکی‌یکی می‌خواند (sequential GET) با fallback خودکار"""
    result = {}
    for i, (key, oid) in enumerate(oids.items()):
        result[key] = snmp_get_with_fallback(ip, oid, community, request_id=i + 1)
    return result
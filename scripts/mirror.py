#!/usr/bin/env python3
"""爱快公开下载与技术云盘的可续传 Release 镜像，只使用 Python 标准库。"""

from __future__ import annotations

import argparse
import atexit
import base64
import configparser
import hashlib
import hmac
import json
import os
import re
import shutil
import secrets
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath

from names import english_filename

ROOT = Path(__file__).resolve().parents[1]
SITE = "https://www.ikuai8.com"
CLOUD = "https://demo.ikuai8.com"
PATCH = "https://patch.ikuai8.com"
UPSTREAM = "https://raw.githubusercontent.com/LucienShui/ikuai-firmware/main"
ARCHIVE = "LOVECHEN/ikuaifw-2512500960-"
INDEX_TAG = "mirror-index"
CATEGORY_TAG = "download-categories"
PART_SIZE = 1900 * 1024 * 1024
CATEGORIES = {
    "AP": "ap", "OLT": "olt", "iKuai_x86_beta": "x86-beta",
    "云平台类": "cloud", "交换机": "switch", "产品素材": "product",
    "功能配合类教程": "tutorial", "升级文件": "upgrade", "历史固件": "history",
    "小工具综合": "tools", "路由": "router", "防火墙": "firewall",
}
META = {"sources.json", "checksums.sha256", "failures.json"}
TRANSFER_PREFIX = "mirror-run-"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def request(url, data=None, method=None, headers=None, attempts=4, context=None, timeout=30):
    headers = {"User-Agent": "ikuai-release-mirror/1.0", **(headers or {})}
    if data is not None:
        data = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(urllib.parse.quote(url, safe=":/?=&%+@"),
                                 data=data, headers=headers, method=method)
    for attempt in range(attempts):
        try:
            return urllib.request.urlopen(req, timeout=timeout, context=context)
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                raise
            time.sleep(min(60, int(exc.headers.get("Retry-After", 5 * (attempt + 1)))))
        except (OSError, TimeoutError):
            if attempt == attempts - 1:
                raise
            time.sleep(2 ** attempt)


def get_json(url, data=None):
    with request(url, data) as response:
        return json.load(response)


def get_text(url):
    with request(url) as response:
        return response.read().decode("utf-8", errors="replace")


def cloud_list(path):
    entries, page = [], 1
    while True:
        result = get_json(CLOUD + "/api/fs/list", {
            "path": path, "password": "", "page": page,
            "per_page": 200, "refresh": False,
        })
        if result.get("code") != 200:
            raise ValueError(f"云盘 {path}: {result.get('message')}")
        data = result["data"]
        chunk = data.get("content") or []
        entries.extend(chunk)
        if not data.get("has_more") and len(entries) >= data.get("total", len(entries)):
            return entries
        if not chunk:
            raise ValueError(f"云盘分页提前结束: {path}, page={page}")
        page += 1


class Links(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs)


def group_for(name, cloud_path=""):
    match = re.fullmatch(r"(iKuai8?|oem)(?:_x(?:32|64))?_(.+?)\.(?:iso|img(?:\.(?:gz|rar))?|gho|bin|patch)", name, re.I)
    if match:
        version = match[2]
        edition = "enterprise" if "Enterprise" in version else ("oem" if match[1].lower() == "oem" else "free")
        return edition + "-" + version.replace("_", "-")
    if "_sysupgrade_" in name:
        return "hardware-" + re.sub(r"[^A-Za-z0-9_.-]", "-", name.split("_sysupgrade_")[0])
    category = cloud_path.strip("/").split("/")[0]
    bucket = int(digest(cloud_path or name)[:8], 16) % 4
    return "resources-" + CATEGORIES.get(category, digest(category)[:12]) + f"-{bucket}"


def candidate(name, url=None, cloud_path=None, size=None, **extra):
    if not name or name in (".", "..") or "/" in name or "\\" in name or any(ord(c) < 32 for c in name):
        raise ValueError(f"非法文件名: {name!r}")
    source = {"url": url} if url else {"cloud_path": cloud_path}
    return {"filename": name, "tag": group_for(name, cloud_path or ""),
            "sources": [{**source, **extra}], "size": size}


def discover_cloud(errors):
    result, pending, visited = [], deque(["/"]), set()
    while pending:
        path = pending.popleft()
        if path in visited:
            continue
        visited.add(path)
        try:
            entries = cloud_list(path)
        except Exception as exc:
            errors.append({"source": CLOUD + path, "error": str(exc)})
            continue
        for entry in entries:
            name = entry["name"]
            if name in (".", "..") or "/" in name:
                raise ValueError("云盘返回非法路径")
            child = path.rstrip("/") + "/" + name
            if entry["is_dir"]:
                pending.append(child)
            else:
                result.append(candidate(name, cloud_path=child, size=entry["size"],
                                        modified=entry.get("modified")))
        print(f"cloud {path}: {len(entries)}, files={len(result)}", flush=True)
    return result


def x86_candidates(version, edition="free"):
    result = []
    if edition == "enterprise":
        version = version.replace("_Enterprise", "").replace("_Build", "_Enterprise_Build")
    for arch in ("x32", "x64"):
        for folder, extension in (("iso", "iso"), ("img", "img.gz"), ("ghost", "gho"), ("patch", "bin"), ("patch", "patch")):
            if edition in ("enterprise", "oem") and extension != "bin":
                continue
            if version.startswith("4.") and extension not in ("iso", "bin"):
                continue
            name = f"{'oem' if edition == 'oem' else 'iKuai8'}_{arch}_{version}.{extension}"
            directory = "ent" if edition == "enterprise" else "3.x/" + folder
            result.append(candidate(name, f"{PATCH}/{directory}/{name}", inferred=True))
    return result


def parse_versions(text):
    config = configparser.ConfigParser(interpolation=None, strict=False)
    config.optionxform = str
    config.read_string(text)
    result = []
    for section in config.sections():
        for key, name in config[section].items():
            if key not in ("firmware", "firmware_x64") or not name.endswith(".bin"):
                continue
            device = re.sub(r"_BETA$", "", section)
            if name.startswith(("iKuai8_", "oem_x")):
                version = re.sub(r"^(?:iKuai8|oem)_x(?:32|64)_|\.bin$", "", name)
                edition = "oem" if name.startswith("oem_") else "enterprise" if "Enterprise" in name else "free"
                result.extend(x86_candidates(version, edition))
            else:
                result.append(candidate(name, f"{PATCH}/firmware/{device}/{name}", inferred=True))
            result.append(candidate(name, "https://devapi.ikuai8.com/firmware/sign-url",
                                    device=device, platform=platform_for(name),
                                    system="community" if device in ("X86", "X86_oem") else "enterprise",
                                    firmware=name))
    for section in ("GLOBAL", "APVER2"):
        if section not in config:
            continue
        for model, version in config[section].items():
            if not model.startswith("IK-") or not re.fullmatch(r"\d+\.\d+\.\d+", version):
                continue
            name = f"{model}_sysupgrade_{version}.bin"
            result.append(candidate(name, "https://devapi.ikuai8.com/firmware/sign-url", inferred=True,
                                    device=model, platform=platform_for(name), system="enterprise", firmware=name))
    return result, config


def discover_web(errors):
    result = []
    for page, edition in (("version_list.txt", "free"), ("version_enterprise_list.txt", "enterprise"), ("version_4_list.txt", "free")):
        try:
            for version in set(get_text(UPSTREAM + "/" + page).splitlines()):
                version = version.strip()
                if re.fullmatch(r"[34]\.[0-9.]+(?:_beta)?_Buil[d]?\d+", version):
                    result.extend(x86_candidates(version, edition))
        except Exception as exc:
            errors.append({"source": UPSTREAM + "/" + page, "error": str(exc)})
    for version in ("3.7.14_Build202408071731", "3.6.5_Build202207280937"):
        result.extend(x86_candidates(version, "oem"))
    urls = [SITE + "/component/download", "https://en.ikuai8.com/firmware.html"] + [
        SITE + f"/index.php?option=com_content&view=article&id={ident}" for ident in (331, 447, 446, 25)
    ] + ["https://download.ikuai8.com/submit3x/Version_all"]
    snapshots = {}
    for url in urls:
        try:
            text = get_text(url)
            snapshots[url] = text
            if "Version_all" in url:
                entries, config = parse_versions(text)
                result.extend(entries)
                devices = [s for s in config.sections() if s not in ("GLOBAL", "APVER2")]
                for device in devices:
                    try:
                        current = FIRMWARE_API.versions(device)
                        snapshots["https://devapi.ikuai8.com/firmware/version-all?model=" + device] = current
                        result.extend(parse_versions(current)[0])
                    except Exception as exc:
                        errors.append({"source": "version-all:" + device, "error": str(exc)})
            else:
                if "en.ikuai8.com" in url:
                    for match in re.finditer(r"var info_([23])_x = (\{[^\n]+\})", text):
                        generation, info = match[1], json.loads(match[2])
                        for arch in ("x86", "x64"):
                            for folder, suffix, ext in (("iso", "iso", "iso"), ("img", "img", "img.rar" if generation == "2" else "img.gz"), ("ghost", "gho", "gho"), ("patch", "patch", "patch"), ("patch", "bin", "bin")):
                                if info.get(arch + "_" + suffix):
                                    name = info[arch + "_prefix"] + "." + ext
                                    base = PATCH + ("/3.x" if generation == "3" else "")
                                    result.append(candidate(name, f"{base}/{folder}/{name}", advertised_md5=info[arch + "_" + suffix]))
                for link in Links(text).links:
                    href = urllib.parse.urljoin(url, link["href"])
                    parsed = urllib.parse.urlsplit(href)
                    if parsed.path == "/download.php":
                        path = urllib.parse.parse_qs(parsed.query).get("n", [""])[0]
                        href = PATCH + "/" + path.lstrip("/")
                        parsed = urllib.parse.urlsplit(href)
                    if re.search(r"\.(?:bin|patch|iso|img\.gz|gho|zip|rar|exe|pdf)$", parsed.path, re.I):
                        name = urllib.parse.unquote(PurePosixPath(parsed.path).name)
                        result.append(candidate(name, href, advertised_md5=link.get("data-md5")))
                # 历史日志提供官方 build，不能从发布日期猜测 build 时间。
                for version in re.findall(r"(?:iKuai8_)?([34]\.[0-9.]+(?:_Enterprise)?_Build\d+)", text):
                    result.extend(x86_candidates(version, "enterprise" if "Enterprise" in version or "id=447" in url else "free"))
        except Exception as exc:
            errors.append({"source": url, "error": str(exc)})
    return result, snapshots


def discover_archive(errors):
    result = []
    try:
        tree = get_json(f"https://api.github.com/repos/{ARCHIVE}/git/trees/main?recursive=1")
        if tree.get("truncated"):
            raise ValueError("GitHub tree 被截断")
        for entry in tree["tree"]:
            name = entry["path"]
            if entry["type"] == "blob" and "/" not in name and name.endswith((".bin", ".iso", ".gho", ".img.gz", ".patch")):
                result.append(candidate(name, f"https://raw.githubusercontent.com/{ARCHIVE}/{tree['sha']}/{name}",
                                        size=entry["size"], git_blob_sha1=entry["sha"], provenance="community-archive"))
    except Exception as exc:
        errors.append({"source": ARCHIVE, "error": str(exc)})
    return result


def merge_candidates(entries):
    groups = {}
    for entry in entries:
        # 固件以原始文件名合并多来源；资料以云盘完整路径区分同名文件。
        path = next((s["cloud_path"] for s in entry["sources"] if "cloud_path" in s), "")
        identity = entry["filename"] if not entry["tag"].startswith("resources-") else path or entry["sources"][0].get("url")
        key = entry["tag"] + "/" + identity
        if key not in groups:
            groups[key] = {**entry, "id": digest(key), "sources": []}
        item = groups[key]
        if entry.get("size") is not None and item.get("size") is None:
            item["size"] = entry["size"]
        for source in entry["sources"]:
            if source not in item["sources"]:
                item["sources"].append(source)
    english_counts = Counter(english_filename(e["filename"], e["id"])[0] for e in groups.values())
    for item in groups.values():
        name = item["filename"]
        base = english_filename(name, item["id"])[0]
        item["asset_name"], missing = english_filename(name, item["id"], english_counts[base] > 1 or base in META)
        if missing:
            item["untranslated_name"] = missing
        else:
            item.pop("untranslated_name", None)
        item["sources"].sort(key=lambda s: ("cloud_path" not in s, bool(s.get("inferred")), s.get("provenance") == "community-archive"))
    return sorted(groups.values(), key=lambda e: (e["tag"], e["asset_name"]))


def legacy_asset_filename(entry):
    name = entry["filename"]
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip(".")[:180] or "file"
    if safe != name or safe in META:
        safe = safe[:140] + "--" + entry["id"][:12] + "".join(PurePosixPath(name).suffixes)[-20:]
    return safe


def release_title(tag):
    if tag == INDEX_TAG:
        return "iKuai 镜像总索引"
    if tag == CATEGORY_TAG:
        return "iKuai 分类下载目录"
    if tag.startswith("resources-"):
        category, bucket = tag.removeprefix("resources-").rsplit("-", 1)
        title = next((key for key, value in CATEGORIES.items() if value == category), category)
        return f"{title} · {int(bucket) + 1}/4"
    if tag.startswith("hardware-"):
        return "iKuai 硬件固件 · " + tag.removeprefix("hardware-")
    edition, version = tag.split("-", 1)
    title = {"free": "免费版", "enterprise": "企业版", "oem": "OEM"}.get(edition, edition)
    return f"iKuai {title} {version}"


def discover(args):
    errors = []
    web, snapshots = discover_web(errors)
    files = discover_cloud(errors) + web + discover_archive(errors)
    if args.previous and Path(args.previous).exists():
        files += json.loads(Path(args.previous).read_text())["files"]
    files = merge_candidates(files)
    # 历史 BIN 同时尝试新签名接口，避免旧 CDN 删除后丢失仍存于官方对象存储的版本。
    for entry in files:
        name = entry["filename"]
        device = None
        if name.endswith(".bin") and name.startswith(("iKuai8_x", "oem_x")):
            device = "X86_oem" if name.startswith("oem_") else "X86ENT" if "Enterprise" in name else "X86"
        elif "_sysupgrade_" in name:
            model = name.split("_sysupgrade_")[0]
            if re.fullmatch(r"(?:IK-)?(?:MT\w+|MVL\w+|IPQ\w+)-[A-Za-z0-9-]+", model):
                device = model.split("-", 2)[-1] if model.startswith("IK-") else model.split("-", 1)[-1]
        if device:
            source = {"url": "https://devapi.ikuai8.com/firmware/sign-url", "device": device,
                      "platform": platform_for(name), "system": "community" if device in ("X86", "X86_oem") else "enterprise",
                      "firmware": name, "inferred": True}
            if not any(s.get("firmware") == name for s in entry["sources"]):
                entry["sources"].append(source)
    catalog = {"schema": 1, "discovered_at": now(), "files": files, "discovery_errors": errors,
               "source_snapshots": snapshots}
    save(args.catalog, catalog)
    summary = {"files": len(files), "release_groups": len({e['tag'] for e in files}),
               "known_bytes": sum(e.get("size") or 0 for e in files),
               "categories": dict(Counter(e["tag"].split("-")[0] for e in files)), "errors": errors}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return bool(errors)


def gh(*args, check=True):
    proc = run_gh(args)
    if check and proc.returncode:
        raise RuntimeError(proc.stderr.strip())
    return proc


def run_gh(args, payload=None):
    network_failures = 0
    for attempt in range(65):
        proc = subprocess.run(["gh", *args], input=payload, capture_output=True, text=True)
        if proc.returncode and any(term in proc.stderr.lower() for term in (
            "eof", "connection reset", "tls handshake timeout", "i/o timeout",
        )):
            network_failures += 1
            if network_failures >= 3:
                return proc
            time.sleep(2 ** network_failures)
            continue
        if not proc.returncode or not any(term in proc.stderr.lower() for term in (
            "rate limit", "abuse detection", "http 429", "http 502", "http 503", "http 504",
        )):
            return proc
        print(f"GitHub 暂时限流，60 秒后重试 ({attempt + 1}/65)", flush=True)
        time.sleep(60)
    return proc


class PublicationError(RuntimeError):
    """发布端失败必须中止分片，不能伪装成源文件不可用。"""


class GitHub:
    def __init__(self, repo):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("非法仓库名")
        self.repo = repo

    def api(self, endpoint, data=None, method=None):
        args = ["api", endpoint]
        if method:
            args += ["--method", method]
        if data is not None:
            proc = run_gh([*args, "--input", "-"], payload=json.dumps(data))
        else:
            proc = gh(*args, check=False)
        if proc.returncode:
            if "HTTP 404" in proc.stderr and data is None and method in (None, "GET"):
                return None
            raise PublicationError(f"GitHub {method or ('POST' if data is not None else 'GET')} {endpoint}: {proc.stderr.strip()}")
        return json.loads(proc.stdout) if proc.stdout.strip() else None

    def release(self, tag):
        return self.api(f"repos/{self.repo}/releases/tags/{urllib.parse.quote(tag, safe='')}")

    def assets(self, release):
        if not release:
            return {}
        result, page = {}, 1
        while True:
            items = self.api(f"repos/{self.repo}/releases/{release['id']}/assets?per_page=100&page={page}")
            result.update({a["name"]: a for a in items})
            if len(items) < 100:
                return result
            page += 1

    def ensure_release(self, tag):
        release = self.release(tag)
        if release:
            return release
        head = self.api(f"repos/{self.repo}/git/ref/heads/main")["object"]["sha"]
        dated_tag(self, tag, head, release_sort_date(tag))
        return self.api(f"repos/{self.repo}/releases", {
            "tag_name": tag, "name": release_title(tag),
            "body": "镜像进行中。完整性与来源详见 sources.json 和 checksums.sha256。",
            "draft": False, "prerelease": is_prerelease(tag),
            "make_latest": "false", "target_commitish": "main",
        })

    def read_asset(self, assets, name, fallback):
        if name not in assets:
            return fallback
        asset = assets[name]
        proc = gh("api", asset["url"], "-H", "Accept: application/octet-stream")
        return json.loads(proc.stdout)

    def upload(self, tag, path, clobber=False):
        args = ["release", "upload", tag, str(path), "--repo", self.repo]
        if clobber:
            args.append("--clobber")
        for attempt in range(4):
            proc = gh(*args, check=False)
            if proc.returncode == 0:
                return
            # 上传超时可能已在服务端完成；由调用者在重跑时通过 digest 恢复。
            if attempt == 3 or any(term in proc.stderr for term in ("already_exists", "HTTP 401", "HTTP 403")):
                raise PublicationError(f"GitHub upload {tag}/{path.name}: {proc.stderr.strip()}")
            time.sleep(5 * (attempt + 1))


def platform_for(name):
    if name.startswith(("iKuai", "oem_x")):
        return "x86-64" if "x64" in name else "x86-32"
    return "mips" if re.search(r"MT762|QCA95|AR934|RTL8197", name) else "arm"


class FirmwareAPI:
    """兼容公开下载客户端的短时签名；证书仅在任务临时目录内使用。"""

    def __init__(self):
        self.gwid = secrets.token_hex(16)
        self.context = None
        self.temp = None

    def signed_headers(self, params):
        raw = "&".join(f"{key}={params[key]}" for key in sorted(params) if params[key])
        signature = hmac.new(self.gwid[-10:].encode(), raw.encode(), hashlib.sha256).hexdigest()
        return {**params, "X-Sign": signature}

    def versions(self, device):
        headers = self.signed_headers({"X-Device-Platform": device, "X-Gw-Id": self.gwid,
                                       "X-Nonce": str(secrets.randbelow(10**10)).zfill(10)})
        with request("https://devapi.ikuai8.com/firmware/version-all", headers=headers) as response:
            result = json.load(response)
        url = (result.get("data") or {}).get("fileUrl")
        if result.get("code") != 0 or not url:
            raise ValueError(f"版本接口: {result.get('code')} {result.get('message')}")
        return get_text(url)

    def resolve(self, source):
        headers = self.signed_headers({
            "X-Device-Platform": source["platform"], "X-Firmware-Name": source["firmware"],
            "X-Gw-Id": self.gwid, "X-Nonce": str(secrets.randbelow(10**10)).zfill(10),
            "X-System-Version": source["system"], "X-Model-Type": source["device"],
        })
        with request("https://devapi.ikuai8.com/firmware/sign-url", method="POST", headers=headers) as response:
            result = json.load(response)
        data = result.get("data") or {}
        if result.get("code") != 0 or not data.get("signUrl") or not data.get("token"):
            raise ValueError(f"固件签名接口: {result.get('code')} {result.get('message')}")
        if self.context is None:
            (ROOT / ".work").mkdir(exist_ok=True)
            self.temp = tempfile.TemporaryDirectory(prefix="ikuai-client-", dir=ROOT / ".work")
            atexit.register(self.temp.cleanup)
            # 固定公开兼容客户端版本，不执行第三方代码，不导入系统 Keychain。
            base = "https://raw.githubusercontent.com/iKuaiPatch/tools/ac974757cc18cc8b3c28b415121e7e7064f953ea/new_download_firmware/mtls/"
            for name in ("ca.crt", "client.crt", "client.key"):
                path = Path(self.temp.name) / name
                path.write_text(get_text(base + name))
                path.chmod(0o600)
            self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            self.context.check_hostname = False
            self.context.verify_mode = ssl.CERT_NONE
            self.context.set_ciphers("ALL:@SECLEVEL=0")
            self.context.load_cert_chain(str(Path(self.temp.name) / "client.crt"), str(Path(self.temp.name) / "client.key"))
        return data["signUrl"], {"X-Auth-Token": str(data["token"]), "X-Gw-Id": self.gwid}, self.context


FIRMWARE_API = FirmwareAPI()


def is_prerelease(tag):
    return bool(re.search(r"(?:^|[-_])(?:beta|rc)\d*(?:[-_]|$)", tag, re.I))


def resolve_source(source):
    if "firmware" in source:
        return FIRMWARE_API.resolve(source)
    if "cloud_path" not in source:
        return source["url"], {}, None
    result = get_json(CLOUD + "/api/fs/get", {"path": source["cloud_path"], "password": ""})
    if result.get("code") != 200 or not result.get("data", {}).get("raw_url"):
        raise ValueError(f"云盘直链解析失败: {result.get('message')}")
    return result["data"]["raw_url"], {}, None


def download(entry, directory):
    errors = []
    target = directory / entry["asset_name"]
    for source in entry["sources"]:
        try:
            url, headers, context = resolve_source(source)
            started = progress = time.monotonic()
            budget = min(1800, max(300, (entry.get("size") or 0) / (128 * 1024)))
            print(f"downloading {entry['tag']}/{entry['asset_name']}", flush=True)
            with request(url, headers=headers, context=context, attempts=2) as response, target.open("wb") as output:
                expected = response.headers.get("Content-Length")
                is_html = entry["filename"].lower().endswith((".html", ".htm"))
                if not is_html and "text/html" in response.headers.get("Content-Type", "").lower():
                    raise ValueError("下载到 HTML 错误页，不是文件")
                sha = hashlib.sha256()
                git_sha = hashlib.sha1()
                if source.get("git_blob_sha1"):
                    git_sha.update(f"blob {entry['size']}\0".encode())
                first, size = b"", 0
                while chunk := response.read1(64 * 1024):
                    current = time.monotonic()
                    if current - started > budget:
                        raise TimeoutError(f"源站下载超过 {budget:.0f} 秒，切换来源或下轮重试")
                    if not first:
                        first = chunk[:2048].lstrip().lower()
                        if not is_html and first.startswith((b"<!doctype html", b"<html")):
                            raise ValueError("下载到 HTML/XML 错误页，不是文件")
                    output.write(chunk)
                    sha.update(chunk)
                    git_sha.update(chunk)
                    size += len(chunk)
                    if current - progress >= 30:
                        print(f"progress {entry['asset_name']}: {size}/{expected or '?'} bytes, {size / max(1, current - started) / 1024:.0f} KiB/s", flush=True)
                        progress = current
            if expected is not None and size != int(expected):
                raise ValueError(f"传输不完整: {size}/{expected}")
            if entry.get("size") is not None and size != entry["size"]:
                raise ValueError(f"源文件大小不一致: {size}/{entry['size']}")
            if size == 0 and entry.get("size") != 0:
                raise ValueError("意外的空文件")
            if source.get("git_blob_sha1") and git_sha.hexdigest() != source["git_blob_sha1"]:
                raise ValueError("Git blob SHA-1 校验失败")
            return target, {"id": entry["id"], "filename": entry["filename"],
                            "sha256": sha.hexdigest(), "size": size,
                            "source": source, "archived_at": now()}
        except Exception as exc:
            errors.append({"source": source, "error": str(exc)})
            target.unlink(missing_ok=True)
    raise ValueError(json.dumps(errors, ensure_ascii=False))


def split_file(path, size=PART_SIZE):
    if path.stat().st_size <= size:
        return [path]
    parts = []
    with path.open("rb") as original:
        number = 1
        while True:
            first = original.read(min(1024 * 1024, size))
            if not first:
                break
            part = path.with_name(path.name + f".part-{number:03d}")
            with part.open("wb") as output:
                output.write(first)
                remaining = size - len(first)
                while remaining:
                    chunk = original.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    output.write(chunk)
                    remaining -= len(chunk)
            parts.append(part)
            number += 1
    return parts


def file_sha(path):
    with path.open("rb") as data:
        return hashlib.file_digest(data, "sha256").hexdigest()


def complete_record(record, assets):
    parts = record.get("assets", [])
    if not parts:
        return False
    return all(p["name"] in assets and assets[p["name"]]["size"] == p["size"]
               and assets[p["name"]].get("digest") == "sha256:" + p["sha256"] for p in parts)


def repair_group_names(client, entries, records, assets):
    changed = False
    for entry in entries:
        record = records.get(entry["id"])
        if record is None:
            old = assets.get(legacy_asset_filename(entry))
            if old is None:
                matches = [a for a in assets.values() if "--" + entry["id"][:12] in a["name"]
                           and a["name"] not in META and a["size"] == entry.get("size")]
                if len(matches) == 1:
                    old = matches[0]
            if old and (old.get("digest") or "").startswith("sha256:") and entry.get("size") in (None, old["size"]):
                record = {"id": entry["id"], "filename": entry["filename"], "size": old["size"],
                          "sha256": old["digest"].removeprefix("sha256:"), "source": entry["sources"][0],
                          "archived_at": old["created_at"], "assets": [{"name": old["name"], "size": old["size"],
                          "sha256": old["digest"].removeprefix("sha256:")}]}
                records[entry["id"]] = record
                changed = True
        if not record:
            continue
        for part in record["assets"]:
            remote = assets.get(part["name"])
            if remote is None:
                matches = [a for a in assets.values() if a.get("digest") == "sha256:" + part["sha256"] and a["size"] == part["size"] and a["name"] not in META]
                if len(matches) == 1:
                    remote = matches[0]
            if remote is None:
                continue
            split = re.search(r"\.part-\d+$", part["name"])
            suffix = split[0] if split else ""
            desired = entry["asset_name"] + suffix
            if remote["name"] != desired or remote.get("label") not in ("", None):
                updated = client.api(f"repos/{client.repo}/releases/assets/{remote['id']}",
                                     {"name": desired, "label": ""}, "PATCH")
                assets.pop(remote["name"], None)
                assets[updated["name"]] = updated
                part["name"] = updated["name"]
                changed = True
    return changed


def release_body(repo, tag, records, failures):
    lines = [f"已归档 **{len(records)}** 个文件，本轮不可用 **{len(failures)}** 项。",
             "", "校验：checksums.sha256；来源与原目录：sources.json。", "",
             "## 文件名对照", "", "| 原始中文名 / 原文件名 | 英文文件名 |", "| --- | --- |"]
    for record in sorted(records.values(), key=lambda r: (r["filename"], r["id"])):
        original = record["filename"].replace("|", "&#124;").replace("\n", " ")
        for part in record["assets"]:
            link = f"https://github.com/{repo}/releases/download/{urllib.parse.quote(tag, safe='')}/{urllib.parse.quote(part['name'], safe='')}"
            lines.append(f"| {original} | [{part['name']}]({link}) |")
    return "\n".join(lines)


def checkpoint(client, tag, records, failures, directory):
    save(directory / "sources.json", {"schema": 1, "updated_at": now(), "files": list(records.values())})
    save(directory / "failures.json", failures)
    lines = sorted(f"{part['sha256']}  {part['name']}\n" for record in records.values() for part in record["assets"])
    (directory / "checksums.sha256").write_text("".join(lines))
    for name in sorted(META):
        client.upload(tag, directory / name, clobber=True)


def sync_group(client, tag, entries, work):
    release = client.release(tag)
    assets = client.assets(release)
    previous = client.read_asset(assets, "sources.json", {"files": []})
    records = {r["id"]: r for r in previous["files"]}
    renamed = repair_group_names(client, entries, records, assets)
    failures, added, unchanged = [], 0, 0
    for entry in entries:
        record = records.get(entry["id"])
        if record and complete_record(record, assets):
            unchanged += 1
            continue
        with tempfile.TemporaryDirectory(prefix="ikuai-file-", dir=work) as temp:
            directory = Path(temp)
            try:
                path, record = download(entry, directory)
                parts = split_file(path)
                if release is None:
                    release = client.ensure_release(tag)
                record["assets"] = []
                for part in parts:
                    part_sha, size = file_sha(part), part.stat().st_size
                    existing = assets.get(part.name)
                    if existing:
                        if existing["size"] != size or existing.get("digest") != "sha256:" + part_sha:
                            raise ValueError(f"同名 Release 文件内容冲突，保留原包: {part.name}")
                    else:
                        client.upload(tag, part)
                    record["assets"].append({"name": part.name, "size": size, "sha256": part_sha})
                records[entry["id"]] = record
                added += 1
                print(f"archived {tag}/{entry['asset_name']} ({record['size']})", flush=True)
                if added % 10 == 0:
                    checkpoint(client, tag, records, failures, work)
            except PublicationError:
                raise
            except Exception as exc:
                failures.append({"id": entry["id"], "filename": entry["filename"],
                                 "known_source": any(not s.get("inferred") for s in entry["sources"]),
                                 "error": str(exc)})
                print(f"unavailable {tag}/{entry['asset_name']}: {exc}", flush=True)
    if release and (added or renamed or failures or not previous["files"]):
        checkpoint(client, tag, records, failures, work)
        client.api(f"repos/{client.repo}/releases/{release['id']}", {"body": release_body(client.repo, tag, records, failures), "name": release_title(tag), "prerelease": is_prerelease(tag)}, "PATCH")
    return {"tag": tag, "added": added, "unchanged": unchanged, "failures": failures,
            "archived": len(records), "bytes": sum(r["size"] for r in records.values())}


def group_priority(tag):
    order = {"enterprise": 4, "free": 3, "oem": 2, "hardware": 1, "resources": 0}
    return order[tag.split("-")[0]], tuple(map(int, re.findall(r"\d+", tag))), tag


def transfer_tag():
    run = os.environ.get("GITHUB_RUN_ID", "")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    if not re.fullmatch(r"[0-9]+", run) or not re.fullmatch(r"[0-9]+", attempt):
        raise ValueError("临时清单必须绑定有效的 Actions run 和 attempt")
    return f"{TRANSFER_PREFIX}{run}-{attempt}"


def transfer_release(client):
    release_id = os.environ.get("TRANSFER_RELEASE_ID", "")
    if not re.fullmatch(r"[0-9]+", release_id):
        raise ValueError("缺少有效的临时 Release ID")
    release = client.api(f"repos/{client.repo}/releases/{release_id}")
    if not release or not release.get("draft") or release["tag_name"] != transfer_tag():
        raise ValueError("临时 Release 不属于本次运行，拒绝读写或删除")
    return release


def transfer_upload(client, release, path):
    payload = path.read_bytes()
    if len(payload) > 10 * 1024 * 1024:
        raise ValueError("临时通道仅允许最多 10 MiB 的 JSON 清单或报告")
    json.loads(payload)
    client.upload(release["tag_name"], path, clobber=True)
    expected = "sha256:" + hashlib.sha256(payload).hexdigest()
    for attempt in range(3):
        asset = client.assets(release).get(path.name)
        if asset and asset.get("state") == "uploaded" and asset.get("size") == len(payload) and asset.get("digest") == expected:
            return
        if attempt < 2:
            time.sleep(2)
    raise PublicationError(f"临时清单上传后校验失败: {path.name}")


def transfer_download(client, asset, path):
    proc = gh("api", asset["url"], "-H", "Accept: application/octet-stream")
    payload = proc.stdout.encode("utf-8")
    if asset.get("state") != "uploaded" or len(payload) != asset["size"] or asset.get("digest") != "sha256:" + hashlib.sha256(payload).hexdigest():
        raise PublicationError(f"临时清单下载校验失败: {asset['name']}")
    json.loads(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def stage_inventory(args):
    path = Path(args.catalog)
    catalog = json.loads(path.read_text())
    if not isinstance(catalog.get("files"), list) or not catalog["files"]:
        raise ValueError("扫描未生成有效清单，不能用旧清单静默替代")
    client = GitHub(args.repo)
    prune_transfer_drafts(client)
    release = client.api(f"repos/{client.repo}/releases", {
        "tag_name": transfer_tag(), "target_commitish": "main", "draft": True,
        "name": "Temporary Actions metadata", "body": json.dumps({"schema": "ikuai-workflow-transfer/v1", "created_at": now()}),
        "make_latest": "false",
    })
    # 即使上传失败，后续收尾也能定位本次创建的草稿。
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a") as handle:
            handle.write(f"release_id={release['id']}\n")
    transfer_upload(client, release, path)
    print(f"清单已发布到临时草稿 {release['id']}，共 {len(catalog['files'])} 项", flush=True)
    return False


def prune_transfer_drafts(client):
    stale, page = [], 1
    while True:
        releases = client.api(f"repos/{client.repo}/releases?per_page=100&page={page}")
        for release in releases:
            if not release.get("draft") or not re.fullmatch(r"mirror-run-\d+-\d+", release["tag_name"]):
                continue
            try:
                marker = json.loads(release.get("body") or "{}")
                created = datetime.fromisoformat(marker["created_at"])
                if marker.get("schema") == "ikuai-workflow-transfer/v1" and (datetime.now(timezone.utc) - created).total_seconds() > 86400:
                    stale.append(release["id"])
            except (ValueError, KeyError, TypeError):
                continue
        if len(releases) < 100:
            break
        page += 1
    for release_id in stale:
        client.api(f"repos/{client.repo}/releases/{release_id}", method="DELETE")
        print(f"已清理超过 1 天的临时草稿 {release_id}", flush=True)


def fetch_inventory(args):
    client = GitHub(args.repo)
    release = transfer_release(client)
    asset = client.assets(release).get(Path(args.catalog).name)
    if not asset:
        raise PublicationError("本轮临时清单不存在")
    transfer_download(client, asset, Path(args.catalog))
    return False


def stage_report(args):
    path = Path(args.report_dir) / f"shard-{args.shard}.json"
    if not path.exists():
        print(f"::warning::分片 {args.shard} 未生成报告，索引将标记未完成")
        return False
    client = GitHub(args.repo)
    transfer_upload(client, transfer_release(client), path)
    return False


def fetch_reports(args):
    client = GitHub(args.repo)
    assets = client.assets(transfer_release(client))
    for shard in range(args.shards):
        name = f"shard-{shard}.json"
        if name not in assets:
            print(f"::warning::本轮缺少分片报告 {shard}，不复用其他运行的报告")
            continue
        transfer_download(client, assets[name], Path(args.report_dir) / name)
    return False


def cleanup_transfer(args):
    client = GitHub(args.repo)
    release = transfer_release(client)
    client.api(f"repos/{client.repo}/releases/{release['id']}", method="DELETE")
    print(f"已删除本轮临时草稿 {release['id']}", flush=True)
    return False


def sync(args):
    if not 0 <= args.shard < args.shards:
        raise ValueError("非法 shard")
    client = GitHub(args.repo)
    catalog = json.loads(Path(args.catalog).read_text())
    groups = defaultdict(list)
    for entry in catalog["files"]:
        if int(digest(entry["tag"])[:8], 16) % args.shards == args.shard:
            groups[entry["tag"]].append(entry)
    results = []
    work = ROOT / ".work" / f"worker-{args.shard}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        for tag, entries in sorted(groups.items(), key=lambda pair: group_priority(pair[0]), reverse=True):
            result = sync_group(client, tag, entries, work)
            results.append(result)
            save(Path(args.report_dir) / f"shard-{args.shard}.json", results)
            print(json.dumps({k: v for k, v in result.items() if k != "failures"}), flush=True)
    finally:
        shutil.rmtree(work)
    return False


def publish(args):
    client = GitHub(args.repo)
    catalog_path = Path(args.catalog)
    catalog = json.loads(catalog_path.read_text())
    reports = [item for path in Path(args.report_dir).rglob("shard-*.json") for item in json.loads(path.read_text())]
    expected = {entry["tag"] for entry in catalog["files"]}
    completed = {r["tag"] for r in reports}
    totals = {"discovered_at": catalog["discovered_at"], "published_at": now(),
              "candidates": len(catalog["files"]), "release_groups": len(expected),
              "processed_groups": len(completed), "archived_files": sum(r["archived"] for r in reports),
              "archived_bytes": sum(r["bytes"] for r in reports),
              "missing_groups": sorted(expected - completed), "discovery_errors": catalog["discovery_errors"],
              "unavailable_files": sum(len(r["failures"]) for r in reports)}
    totals["unavailable_known_files"] = sum(bool(f.get("known_source")) for r in reports for f in r["failures"])
    with tempfile.TemporaryDirectory(prefix="ikuai-index-", dir=ROOT / ".work") as temp:
        directory = Path(temp)
        shutil.copyfile(catalog_path, directory / "catalog.json")
        save(directory / "status.json", totals)
        save(directory / "reports.json", reports)
        lines = ["# iKuai 镜像索引", "", f"更新时间：{now()}", "", "```json", json.dumps(totals, ensure_ascii=False, indent=2), "```", "", "| Release | 文件 | 字节 | 本轮不可用 |", "| --- | ---: | ---: | ---: |"]
        for r in sorted(reports, key=lambda r: (build_date(r["tag"]) or "", r["tag"]), reverse=True):
            lines.append(f"| [{r['tag']}](https://github.com/{client.repo}/releases/tag/{r['tag']}) | {r['archived']} | {r['bytes']} | {len(r['failures'])} |")
        (directory / "INDEX.md").write_text("\n".join(lines) + "\n")
        release = client.ensure_release(INDEX_TAG)
        for path in sorted(directory.iterdir()):
            client.upload(INDEX_TAG, path, clobber=True)
        body = ("爱快全量镜像总索引。\n\n"
                f"已发现 **{totals['candidates']}** 个候选文件；已归档 **{totals['archived_files']}** 个文件 "
                f"（{totals['archived_bytes'] / 1024**3:.2f} GiB）。\n\n"
                f"本轮不可用 **{totals['unavailable_files']}** 项，未完成分组 **{len(totals['missing_groups'])}**。\n\n"
                "下载 `INDEX.md` 浏览分类与版本；`catalog.json` 是累积来源清单；"
                "`status.json` 是覆盖情况；`reports.json` 记录失败项。候选直链不代表实际可用。")
        client.api(f"repos/{client.repo}/releases/{release['id']}", {"body": body, "make_latest": "true"}, "PATCH")
    print(json.dumps(totals, ensure_ascii=False, indent=2))
    refresh_categories(args)
    return bool(totals["missing_groups"] or totals["discovery_errors"] or totals["unavailable_known_files"])


def category_body(repo, releases):
    groups = defaultdict(list)
    for release in releases:
        tag = release["tag_name"]
        if release.get("draft") or tag in (INDEX_TAG, CATEGORY_TAG):
            continue
        category = tag.split("-", 1)[0]
        if category == "free":
            version = re.match(r"free-([234])\.", tag)
            category = "free-" + version[1] if version else "free-other"
        elif category == "resources":
            category = tag.rsplit("-", 1)[0]
        groups[category].append(release)
        if tag.startswith("enterprise-4.") and is_prerelease(tag):
            groups["enterprise-4-beta"].append(release)
    sections = [("free", "免费版"), ("free-2", "免费版 2.x"), ("free-3", "免费版 3.x"),
                ("free-4", "免费版 4.x"), ("free-other", "其他免费版"),
                ("enterprise", "企业版"), ("enterprise-4-beta", "企业版 4.x Beta"),
                ("oem", "OEM"), ("hardware", "硬件固件")]
    sections += [("resources-" + code, title) for title, code in CATEGORIES.items()]
    lines = ["# 按类型下载", "", "点击版本进入 Release，选择 x32 / x64 或所需格式。只列出已发布的分组，空分类仍在补齐。", ""]
    for slug, title in sections:
        lines += [f'<a name="{slug}"></a>', f"## {title}", ""]
        if slug == "free":
            lines += ["[2.x](#user-content-free-2) · [3.x](#user-content-free-3) · [4.x](#user-content-free-4) · [其他](#user-content-free-other)", ""]
            continue
        entries = sorted(groups[slug], key=lambda r: (r.get("created_at", ""), r["tag_name"]), reverse=True)
        if not entries:
            lines += ["暂无已发布镜像。", ""]
            continue
        for release in entries:
            tag = release["tag_name"]
            title = (release.get("name") or tag).replace("|", "&#124;").replace("\n", " ")
            lines.append(f"- [{title}](https://github.com/{repo}/releases/tag/{urllib.parse.quote(tag, safe='')})")
        lines.append("")
    return "\n".join(lines)


def refresh_categories(args):
    client = GitHub(args.repo)
    releases, page = [], 1
    while True:
        batch = client.api(f"repos/{client.repo}/releases?per_page=100&page={page}")
        releases.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    release = client.ensure_release(CATEGORY_TAG)
    client.api(f"repos/{client.repo}/releases/{release['id']}",
               {"name": release_title(CATEGORY_TAG), "body": category_body(client.repo, releases)}, "PATCH")
    return False


def jitter(args):
    if os.environ.get("GITHUB_EVENT_NAME") != "schedule":
        return False
    delay = secrets.randbelow(1801)
    print(f"定时扫描随机延迟 {delay} 秒", flush=True)
    time.sleep(delay)
    return False


def runs_to_delete(runs, current_run=""):
    if not runs:
        return []
    newest = max(runs, key=lambda run: (run["created_at"], int(run["id"])))["id"]
    return [run["id"] for run in runs if run["id"] != newest
            and str(run["id"]) != str(current_run) and run["status"] == "completed"]


def prune_runs(args):
    client = GitHub(args.repo)
    runs, page = [], 1
    while True:
        result = client.api(f"repos/{client.repo}/actions/runs?per_page=100&page={page}")
        batch = result["workflow_runs"]
        runs.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    for run_id in runs_to_delete(runs, os.environ.get("GITHUB_RUN_ID", "")):
        client.api(f"repos/{client.repo}/actions/runs/{run_id}", method="DELETE")
        print(f"已清理旧 Actions 记录 {run_id}", flush=True)
    return False


def root_commit_needed(commit, current_time=None):
    date = datetime.fromisoformat(commit["committer"]["date"].replace("Z", "+00:00"))
    age = ((current_time or datetime.now(timezone.utc)) - date).days
    return bool(commit["parents"]) or age >= 50


def git_remote_env():
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ValueError("维护 Git 历史需要 GH_TOKEN")
    env = os.environ.copy()
    # 认证只通过子进程环境传递，不落到配置文件或命令参数。
    position = int(env.get("GIT_CONFIG_COUNT", "0"))
    env["GIT_CONFIG_COUNT"] = str(position + 1)
    env[f"GIT_CONFIG_KEY_{position}"] = "http.https://github.com/.extraheader"
    encoded = base64.b64encode(("x-access-token:" + token).encode()).decode()
    env[f"GIT_CONFIG_VALUE_{position}"] = "AUTHORIZATION: basic " + encoded
    return env


def git_run(arguments, env, input_text=None):
    result = subprocess.run(["git", *arguments], cwd=ROOT, env=env, input=input_text,
                            capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    return result.stdout.strip()


def build_date(value):
    match = re.search(r"Build(\d{12}|\d{8})(?!\d)", value)
    if not match:
        return None
    try:
        stamp = datetime.strptime(match[1], "%Y%m%d%H%M" if len(match[1]) == 12 else "%Y%m%d")
        return stamp.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return None


def release_sort_date(tag, assets=()):
    if tag == INDEX_TAG:
        return None
    dates = [date for value in [tag, *(a["name"] for a in assets)] if (date := build_date(value))]
    # 无 Build 日期的资料归到列表末尾，不能冒充最新固件。
    return max(dates, default="1970-01-01T00:00:00+00:00")


def dated_tag(client, tag, root, date, existing=None):
    endpoint = f"repos/{client.repo}/git/refs/tags/{urllib.parse.quote(tag, safe='')}"
    existing = existing or client.api(endpoint.replace("/git/refs/", "/git/ref/"))
    if date is None:
        if not existing or existing["object"]["sha"] != root:
            client.api(endpoint if existing else f"repos/{client.repo}/git/refs",
                       {"sha": root, "force": True} if existing else {"ref": "refs/tags/" + tag, "sha": root},
                       "PATCH" if existing else "POST")
        return
    if existing and existing["object"]["type"] == "tag":
        annotation = client.api(f"repos/{client.repo}/git/tags/{existing['object']['sha']}")
        old_date = datetime.fromisoformat(annotation["tagger"]["date"].replace("Z", "+00:00"))
        if annotation["object"]["sha"] == root and old_date == datetime.fromisoformat(date):
            return
    annotation = client.api(f"repos/{client.repo}/git/tags", {
        "tag": tag, "message": "Mirror sort date: firmware Build time (Asia/Shanghai); undated files sort last.",
        "object": root, "type": "commit", "tagger": {
            "name": "iKuai mirror", "email": "41898282+github-actions[bot]@users.noreply.github.com", "date": date,
        },
    })
    client.api(endpoint if existing else f"repos/{client.repo}/git/refs",
               {"sha": annotation["sha"], "force": True} if existing else {"ref": "refs/tags/" + tag, "sha": annotation["sha"]},
               "PATCH" if existing else "POST")


def retarget_release_tags(client, root):
    releases, page = [], 1
    while True:
        batch = client.api(f"repos/{client.repo}/releases?per_page=100&page={page}")
        releases.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    # 改标签日期会改变分页顺序，必须先取得完整快照。
    for index, release in enumerate(releases):
        if release.get("draft"):
            continue
        if index % 10 == 0:
            current = client.api(f"repos/{client.repo}/git/ref/heads/main")["object"]["sha"]
            if current != root:
                print("main 已有并发更新，停止调整标签，交由下一轮维护处理", flush=True)
                return
        tag = release["tag_name"]
        assets = client.assets(release).values() if tag.startswith("hardware-") else ()
        dated_tag(client, tag, root, release_sort_date(tag, assets))


def compact_history(args):
    client = GitHub(args.repo)
    remote = f"https://github.com/{client.repo}.git"
    for attempt in range(3):
        head = client.api(f"repos/{client.repo}/git/ref/heads/main")["object"]["sha"]
        commit = client.api(f"repos/{client.repo}/git/commits/{head}")
        root = head
        if root_commit_needed(commit):
            env = git_remote_env()
            for role in ("AUTHOR", "COMMITTER"):
                env[f"GIT_{role}_NAME"] = commit["author"]["name"]
                env[f"GIT_{role}_EMAIL"] = commit["author"]["email"]
            git_run(["fetch", "--no-tags", "--depth=1", remote, head], env)
            root = git_run(["commit-tree", commit["tree"]["sha"]], env,
                           "chore: maintain iKuai release mirror [skip actions]\n")
            try:
                git_run(["push", f"--force-with-lease=refs/heads/main:{head}",
                         remote, f"{root}:refs/heads/main"], env)
            except RuntimeError as exc:
                if attempt < 2 and any(term in str(exc).lower() for term in ("stale info", "[rejected]", "fetch first")):
                    continue
                raise
            print(f"main 已维护为单个根提交 {root}", flush=True)
        retarget_release_tags(client, root)
        return False
    raise RuntimeError("main 持续并发更新，本轮未压缩")


def maintain(args):
    refresh_categories(args)
    compact_history(args)
    return prune_runs(args)


def repair_names(args):
    client = GitHub(args.repo)
    catalog = json.loads(Path(args.catalog).read_text())
    entries = merge_candidates(catalog["files"])
    groups = defaultdict(list)
    for entry in entries:
        groups[entry["tag"]].append(entry)
    (ROOT / ".work").mkdir(exist_ok=True)
    page = 1
    while True:
        releases = client.api(f"repos/{client.repo}/releases?per_page=100&page={page}")
        for release in releases:
            tag = release["tag_name"]
            if tag not in groups or (args.tag and tag not in args.tag):
                continue
            assets = client.assets(release)
            previous = client.read_asset(assets, "sources.json", {"files": []})
            failures = client.read_asset(assets, "failures.json", [])
            records = {r["id"]: r for r in previous["files"]}
            if repair_group_names(client, groups[tag], records, assets):
                with tempfile.TemporaryDirectory(prefix="ikuai-rename-", dir=ROOT / ".work") as temp:
                    checkpoint(client, tag, records, failures, Path(temp))
                print(f"repaired {tag}: {len(records)} files", flush=True)
            client.api(f"repos/{client.repo}/releases/{release['id']}",
                       {"body": release_body(client.repo, tag, records, failures),
                        "name": release_title(tag), "prerelease": is_prerelease(tag)}, "PATCH")
        if len(releases) < 100:
            break
        page += 1
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["discover", "sync", "publish", "maintain", "prune-runs", "compact-history", "repair-names", "jitter", "refresh-categories", "stage-inventory", "fetch-inventory", "stage-report", "fetch-reports", "cleanup-transfer"])
    parser.add_argument("--catalog", default=".work/catalog.json")
    parser.add_argument("--previous")
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "LOVECHEN/ikuai-release"))
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--report-dir", default=".work/reports")
    args = parser.parse_args()
    commands = {"discover": discover, "sync": sync, "publish": publish, "maintain": maintain,
                "prune-runs": prune_runs, "compact-history": compact_history,
                "repair-names": repair_names, "jitter": jitter, "refresh-categories": refresh_categories,
                "stage-inventory": stage_inventory, "fetch-inventory": fetch_inventory,
                "stage-report": stage_report, "fetch-reports": fetch_reports, "cleanup-transfer": cleanup_transfer}
    raise SystemExit(int(commands[args.command](args)))


if __name__ == "__main__":
    main()

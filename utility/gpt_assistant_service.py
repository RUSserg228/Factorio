#!/usr/bin/env python3
"""Local companion utility for the Factorio GPT assistant mod.

This script provides a lightweight HTTP service that proxies requests between the
Factorio mod and the OpenAI REST API. It also offers a CLI for first-run setup,
including consent text, API key entry, and connection diagnostics.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional

import requests

CONFIG_DIR = Path.home() / ".factorio-gpt"
CONFIG_PATH = CONFIG_DIR / "config.json"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3925
OPENAI_API_BASE = "https://api.openai.com/v1"
CONSENT_TEXT = (
    "Данные ваших фабрик (сущности, ленты, жидкости, сигналы, инвентари) и история "
    "чатов будут отправляться во внешнюю модель OpenAI для анализа и улучшения "
    "ответов. Продолжая, вы соглашаетесь на передачу этой информации."
)


@dataclass
class RateLimitInfo:
    remaining_requests: Optional[int] = None
    remaining_tokens: Optional[int] = None
    reset_timestamp: Optional[float] = None
    model: Optional[str] = None

    def to_dict(self) -> Dict[str, Optional[float]]:
        return {
            "model": self.model,
            "remaining_requests": self.remaining_requests,
            "remaining_tokens": self.remaining_tokens,
            "reset_timestamp": self.reset_timestamp,
        }


@dataclass
class ServiceConfig:
    api_key: Optional[str] = None
    organization: Optional[str] = None
    default_model: str = "gpt-4o"
    profiles: Dict[str, Dict[str, object]] = field(
        default_factory=lambda: {
            "gpt-4o": {"temperature": 0.4, "max_tokens": 2048},
            "gpt-4.1": {"temperature": 0.2, "max_tokens": 2048},
            "gpt-4.1-mini": {"temperature": 0.3, "max_tokens": 1024},
        }
    )
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    tls_enabled: bool = False
    consent_acknowledged: bool = False

    def to_json(self) -> str:
        data = {
            "api_key": self._encode_secret(self.api_key),
            "organization": self.organization,
            "default_model": self.default_model,
            "profiles": self.profiles,
            "host": self.host,
            "port": self.port,
            "tls_enabled": self.tls_enabled,
            "consent_acknowledged": self.consent_acknowledged,
        }
        return json.dumps(data, ensure_ascii=False, indent=2)

    @staticmethod
    def _encode_secret(secret: Optional[str]) -> Optional[str]:
        if not secret:
            return None
        return base64.b64encode(secret.encode("utf-8")).decode("ascii")

    @staticmethod
    def _decode_secret(secret: Optional[str]) -> Optional[str]:
        if not secret:
            return None
        try:
            return base64.b64decode(secret.encode("ascii")).decode("utf-8")
        except Exception:
            return None

    @classmethod
    def load(cls) -> "ServiceConfig":
        if not CONFIG_PATH.exists():
            return cls()
        with CONFIG_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        cfg = cls()
        cfg.api_key = cls._decode_secret(data.get("api_key"))
        cfg.organization = data.get("organization")
        cfg.default_model = data.get("default_model", cfg.default_model)
        cfg.profiles = data.get("profiles", cfg.profiles)
        cfg.host = data.get("host", cfg.host)
        cfg.port = data.get("port", cfg.port)
        cfg.tls_enabled = data.get("tls_enabled", cfg.tls_enabled)
        cfg.consent_acknowledged = data.get("consent_acknowledged", False)
        return cfg

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with CONFIG_PATH.open("w", encoding="utf-8") as fh:
            fh.write(self.to_json())


class GPTService:
    def __init__(self, config: ServiceConfig):
        self.config = config
        self.rate_limit = RateLimitInfo()
        self._lock = threading.Lock()

    def update_rate_limit(self, headers: Dict[str, str], model: str) -> None:
        rl = RateLimitInfo(model=model)
        for key, value in headers.items():
            key_lower = key.lower()
            if key_lower == "x-ratelimit-remaining-requests":
                try:
                    rl.remaining_requests = int(value)
                except ValueError:
                    rl.remaining_requests = None
            elif key_lower == "x-ratelimit-remaining-tokens":
                try:
                    rl.remaining_tokens = int(value)
                except ValueError:
                    rl.remaining_tokens = None
            elif key_lower == "x-ratelimit-reset-requests":
                try:
                    rl.reset_timestamp = time.time() + float(value)
                except ValueError:
                    rl.reset_timestamp = None
        with self._lock:
            self.rate_limit = rl

    def get_rate_limit(self) -> RateLimitInfo:
        with self._lock:
            return RateLimitInfo(
                remaining_requests=self.rate_limit.remaining_requests,
                remaining_tokens=self.rate_limit.remaining_tokens,
                reset_timestamp=self.rate_limit.reset_timestamp,
                model=self.rate_limit.model,
            )

    def ensure_ready(self) -> None:
        if not self.config.api_key:
            raise RuntimeError("API key not configured. Запустите скрипт с флагом --setup.")
        if not self.config.consent_acknowledged:
            raise RuntimeError("Consent not acknowledged. Run with --setup to accept.")

    def call_openai(self, payload: Dict[str, object]) -> Dict[str, object]:
        self.ensure_ready()
        model = payload.get("model") or self.config.default_model
        payload.setdefault("model", model)
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        if self.config.organization:
            headers["OpenAI-Organization"] = self.config.organization
        response = requests.post(
            f"{OPENAI_API_BASE}/chat/completions",
            headers=headers,
            json=payload,
            timeout=90,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"OpenAI error {response.status_code}: {response.text}")
        self.update_rate_limit(response.headers, model)
        return response.json()

    def check_key(self) -> bool:
        if not self.config.api_key:
            return False
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
        }
        if self.config.organization:
            headers["OpenAI-Organization"] = self.config.organization
        response = requests.get(f"{OPENAI_API_BASE}/models", headers=headers, timeout=30)
        if response.status_code == 200:
            return True
        raise RuntimeError(
            f"Не удалось подтвердить ключ (status={response.status_code}): {response.text}"
        )


service_instance: Optional[GPTService] = None


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "FactorioGPTService/1.0"

    def _set_headers(self, status: int = HTTPStatus.OK, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def _read_json(self) -> Dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(data.decode("utf-8"))
        except json.JSONDecodeError:
            raise ValueError("Invalid JSON payload")

    def _write_json(self, payload: Dict[str, object], status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/status":
            cfg = service_instance.config
            try:
                service_instance.ensure_ready()
                ready = True
                error = None
            except RuntimeError as exc:
                ready = False
                error = str(exc)
            rl = service_instance.get_rate_limit()
            payload = {
                "ready": ready,
                "error": error,
                "host": cfg.host,
                "port": cfg.port,
                "default_model": cfg.default_model,
                "profiles": cfg.profiles,
                "rate_limit": rl.to_dict(),
            }
            self._write_json(payload)
        else:
            self._write_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/chat":
            try:
                payload = self._read_json()
                result = service_instance.call_openai(payload)
                response_payload = {
                    "result": result,
                    "rate_limit": service_instance.get_rate_limit().to_dict(),
                }
                self._write_json(response_payload)
            except Exception as exc:  # noqa: BLE001
                self._write_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
        elif self.path == "/config":
            try:
                payload = self._read_json()
                cfg = service_instance.config
                if "default_model" in payload:
                    cfg.default_model = str(payload["default_model"])
                if "profiles" in payload:
                    cfg.profiles = payload["profiles"]
                if "consent_acknowledged" in payload:
                    cfg.consent_acknowledged = bool(payload["consent_acknowledged"])
                cfg.save()
                self._write_json({"status": "ok"})
            except Exception as exc:  # noqa: BLE001
                self._write_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        else:
            self._write_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
        sys.stdout.write("[HTTP] " + fmt % args + "\n")


class ServiceRunner:
    def __init__(self, config: ServiceConfig):
        self.config = config
        self.httpd: Optional[ThreadingHTTPServer] = None

    def start(self) -> None:
        global service_instance
        service_instance = GPTService(self.config)
        server_address = (self.config.host, self.config.port)
        self.httpd = ThreadingHTTPServer(server_address, RequestHandler)
        print(
            f"Factorio GPT service listening on http://{self.config.host}:{self.config.port} "
            f"(model={self.config.default_model})"
        )
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            print("Service stopped.")


def prompt_consent(config: ServiceConfig) -> bool:
    print("=== Условия использования мода ===")
    print(CONSENT_TEXT)
    answer = input("Продолжить? [y/N]: ").strip().lower()
    if answer not in {"y", "yes", "д", "да"}:
        print("Согласие не дано. Настройка прервана.")
        return False
    config.consent_acknowledged = True
    return True


def prompt_api_key(config: ServiceConfig) -> None:
    api_key = getpass.getpass("Введите OpenAI API ключ: ").strip()
    if not api_key:
        raise ValueError("Ключ не может быть пустым")
    config.api_key = api_key
    org = input("ID организации (Enter, если нет): ").strip()
    config.organization = org or None
    model = input(f"Модель по умолчанию [{config.default_model}]: ").strip() or config.default_model
    if model not in config.profiles:
        config.profiles.setdefault(model, {"temperature": 0.4, "max_tokens": 2048})
    config.default_model = model


def run_setup(config: ServiceConfig) -> None:
    print("Настройка локального сервиса Factorio GPT\n")
    if not prompt_consent(config):
        return
    prompt_api_key(config)
    config.save()
    service = GPTService(config)
    print("Проверка подключения к OpenAI…")
    if service.check_key():
        print("Подключение успешно подтверждено.")
    else:
        print("Не удалось подтвердить ключ. Проверьте настройки.")


def run_status(config: ServiceConfig) -> None:
    print("Текущая конфигурация:")
    print(config.to_json())


def reset_config() -> None:
    if CONFIG_PATH.exists():
        CONFIG_PATH.unlink()
        print("Конфигурация удалена.")
    else:
        print("Конфигурация отсутствует.")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Factorio GPT companion service")
    parser.add_argument("--setup", action="store_true", help="Первичный запуск и ввод API ключа")
    parser.add_argument("--status", action="store_true", help="Показать текущие настройки")
    parser.add_argument("--reset", action="store_true", help="Удалить конфигурацию")
    parser.add_argument("--host", type=str, help="Адрес прослушивания сервиса")
    parser.add_argument("--port", type=int, help="Порт сервиса")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    config = ServiceConfig.load()

    if args.host:
        config.host = args.host
    if args.port:
        config.port = args.port

    if args.reset:
        reset_config()
        return 0

    if args.setup:
        run_setup(config)
        return 0

    if args.status:
        run_status(config)
        return 0

    runner = ServiceRunner(config)

    def handle_sigterm(signum: int, frame: Optional[object]) -> None:  # noqa: D401
        runner.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)
    try:
        runner.start()
    except RuntimeError as exc:
        print(f"Ошибка запуска: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

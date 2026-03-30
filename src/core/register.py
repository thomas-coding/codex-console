"""
注册流程引擎
从 main.py 中提取并重构的注册流程
"""

import base64
import html
import re
import json
import os
import time
import logging
import secrets
import string
import urllib.parse
import uuid
from typing import Optional, Dict, Any, Tuple, Callable, List
from dataclasses import dataclass
from datetime import datetime

from curl_cffi import requests as cffi_requests

from .openai.oauth import OAuthManager, OAuthStart
from .browser_profile import BrowserProfile
from .http_client import OpenAIHTTPClient, HTTPClientError
from ..services import EmailServiceFactory, BaseEmailService, EmailServiceType
from ..database import crud
from ..database.session import get_db
from ..config.constants import (
    OPENAI_API_ENDPOINTS,
    OPENAI_PAGE_TYPES,
    generate_random_user_info,
    OTP_CODE_PATTERN,
    DEFAULT_PASSWORD_LENGTH,
    PASSWORD_CHARSET,
    AccountStatus,
    TaskStatus,
)
from ..config.settings import get_settings


logger = logging.getLogger(__name__)
ACCOUNT_OUTLOOK_RECOVERY_KEY = "outlook_recovery"


@dataclass
class RegistrationResult:
    """注册结果"""
    success: bool
    email: str = ""
    password: str = ""  # 注册密码
    account_id: str = ""
    workspace_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    session_token: str = ""  # 会话令牌
    device_id: str = ""  # oai-did
    error_message: str = ""
    logs: list = None
    metadata: dict = None
    source: str = "register"  # 'register' 或 'login'，区分账号来源

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "success": self.success,
            "email": self.email,
            "password": self.password,
            "account_id": self.account_id,
            "workspace_id": self.workspace_id,
            "access_token": self.access_token[:20] + "..." if self.access_token else "",
            "refresh_token": self.refresh_token[:20] + "..." if self.refresh_token else "",
            "id_token": self.id_token[:20] + "..." if self.id_token else "",
            "session_token": self.session_token[:20] + "..." if self.session_token else "",
            "device_id": self.device_id,
            "error_message": self.error_message,
            "logs": self.logs or [],
            "metadata": self.metadata or {},
            "source": self.source,
        }


@dataclass
class SignupFormResult:
    """提交注册表单的结果"""
    success: bool
    page_type: str = ""  # 响应中的 page.type 字段
    is_existing_account: bool = False  # 是否为已注册账号
    response_data: Dict[str, Any] = None  # 完整的响应数据
    error_message: str = ""


class RegistrationEngine:
    """
    注册引擎
    负责协调邮箱服务、OAuth 流程和 OpenAI API 调用
    """

    def __init__(
        self,
        email_service: BaseEmailService,
        proxy_url: Optional[str] = None,
        callback_logger: Optional[Callable[[str], None]] = None,
        task_uuid: Optional[str] = None,
        browser_profile: Optional[BrowserProfile] = None,
        token_required_for_success: bool = False,
    ):
        """
        初始化注册引擎

        Args:
            email_service: 邮箱服务实例
            proxy_url: 代理 URL
            callback_logger: 日志回调函数
            task_uuid: 任务 UUID（用于数据库记录）
            browser_profile: 可选的统一浏览器画像；未提供时回退原有逻辑
            token_required_for_success: 兼容服务器路由的严格模式开关；当前实现不强制依赖
        """
        self.email_service = email_service
        self.proxy_url = proxy_url
        self.callback_logger = callback_logger or (lambda msg: logger.info(msg))
        self.task_uuid = task_uuid
        self.browser_profile = browser_profile
        self.token_required_for_success = bool(token_required_for_success)

        # 创建 HTTP 客户端
        self.http_client = OpenAIHTTPClient(proxy_url=proxy_url, browser_profile=browser_profile)

        # 创建 OAuth 管理器
        settings = get_settings()
        self.oauth_manager = OAuthManager(
            client_id=settings.openai_client_id,
            auth_url=settings.openai_auth_url,
            token_url=settings.openai_token_url,
            redirect_uri=settings.openai_redirect_uri,
            scope=settings.openai_scope,
            proxy_url=proxy_url  # 传递代理配置
        )
        entry_flow = str(getattr(settings, "registration_entry_flow", "native") or "native").strip().lower()
        # 配置层仅保留 native/abcard；Outlook 邮箱在执行时自动切换 outlook 链路。
        self.registration_entry_flow: str = entry_flow if entry_flow in {"native", "abcard"} else "native"
        self.registration_browser_first_enabled: bool = bool(
            getattr(settings, "registration_browser_first_enabled", False)
        )
        self.registration_browser_headless: bool = bool(
            getattr(settings, "registration_browser_headless", True)
        )
        self.registration_browser_persistent_profile_dir: str = str(
            getattr(settings, "registration_browser_persistent_profile_dir", "") or ""
        ).strip()

        # 状态变量
        self.email: Optional[str] = None
        self.inbox_email: Optional[str] = None  # 邮箱服务原始地址（用于收件）
        self.password: Optional[str] = None  # 注册密码
        self.email_info: Optional[Dict[str, Any]] = None
        self.oauth_start: Optional[OAuthStart] = None
        self.session: Optional[cffi_requests.Session] = None
        self.session_token: Optional[str] = None  # 会话令牌
        self.device_id: Optional[str] = None  # oai-did
        self.logs: list = []
        self._otp_sent_at: Optional[float] = None  # OTP 发送时间戳
        self._is_existing_account: bool = False  # 是否为已注册账号（用于自动登录）
        self._token_acquisition_requires_login: bool = False  # 新注册账号需要二次登录拿 token
        self._create_account_continue_url: Optional[str] = None  # create_account 返回的 continue_url（ABCard链路兜底）
        self._create_account_workspace_id: Optional[str] = None
        self._create_account_account_id: Optional[str] = None
        self._create_account_refresh_token: Optional[str] = None
        self._create_account_completed: bool = False
        self._last_validate_otp_continue_url: Optional[str] = None
        self._last_validate_otp_workspace_id: Optional[str] = None
        self._last_email_otp_start_url: Optional[str] = None
        self._last_validate_otp_page_type: str = ""
        self._last_auth_cookie_has_workspace: bool = False
        self._last_auth_cookie_workspace_id: str = ""
        self._last_register_password_error: Optional[str] = None
        self._last_otp_validation_code: Optional[str] = None
        self._last_otp_validation_status_code: Optional[int] = None
        self._last_otp_validation_outcome: str = ""  # success/http_non_200/network_timeout/network_error

    def _log(self, message: str, level: str = "info"):
        """记录日志"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_message = f"[{timestamp}] {message}"

        # 添加到日志列表
        self.logs.append(log_message)

        # 调用回调函数
        if self.callback_logger:
            self.callback_logger(log_message)

        # 记录到数据库（如果有关联任务）
        if self.task_uuid:
            try:
                with get_db() as db:
                    crud.append_task_log(db, self.task_uuid, log_message)
            except Exception as e:
                logger.warning(f"记录任务日志失败: {e}")

        # 根据级别记录到日志系统
        if level == "error":
            logger.error(message)
        elif level == "warning":
            logger.warning(message)
        else:
            logger.info(message)

    @staticmethod
    def _build_cookie_text_from_browser_items(browser_cookies: Any) -> str:
        cookie_map: dict[str, str] = {}
        order: list[str] = []
        for item in browser_cookies or []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("name") or "").strip()
            val = str(item.get("value") or "").strip()
            if not key:
                continue
            if key not in cookie_map:
                order.append(key)
                cookie_map[key] = val
                continue
            prev = str(cookie_map.get(key) or "").strip()
            if (not prev and val) or (val and len(val) > len(prev)):
                cookie_map[key] = val
        return "; ".join(f"{key}={cookie_map.get(key, '')}" for key in order if key)

    def _merge_browser_cookies_into_session(self, browser_cookies: Any) -> None:
        if self.session is None:
            self.session = self.http_client.session
        if self.session is None:
            return

        for item in browser_cookies or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "").strip()
            if not name:
                continue
            domain = str(item.get("domain") or "").strip() or None
            path = str(item.get("path") or "").strip() or "/"
            try:
                self.session.cookies.set(name, value, domain=domain, path=path)
                continue
            except Exception:
                pass
            try:
                self.session.cookies[name] = value
            except Exception:
                continue

    def _browser_reference_smart_fill(self, page: Any, selector: str, value: str, *, click_first: bool = False) -> bool:
        try:
            elements = page.eles(selector, timeout=8)
            target_ele = None
            for ele in elements or []:
                try:
                    if ele.wait.displayed(timeout=2):
                        target_ele = ele
                        break
                except Exception:
                    continue
            if target_ele is None:
                return False

            if click_first:
                target_ele.click()
                time.sleep(0.3)
            else:
                try:
                    page.run_js("arguments[0].focus();", target_ele)
                except Exception:
                    pass
                time.sleep(0.2)
                target_ele.click()

            page.actions.key_down("CONTROL").type("a").key_up("CONTROL").type("\ue003")
            time.sleep(0.2)

            for char in str(value or ""):
                page.actions.type(char)
                time.sleep(0.05)

            current_value = ""
            try:
                current_value = str(getattr(target_ele, "value", "") or "")
            except Exception:
                current_value = ""
            if current_value != str(value or ""):
                safe_value = json.dumps(str(value or ""))
                page.run_js(f"arguments[0].value = {safe_value};", target_ele)
                page.run_js('arguments[0].dispatchEvent(new Event("input", { bubbles: true }));', target_ele)
                page.run_js('arguments[0].dispatchEvent(new Event("change", { bubbles: true }));', target_ele)
            return True
        except Exception as e:
            self._log(f"浏览器 FSM 输入注入异常: {e}", "warning")
            return False

    def _try_run_outlook_browser_reference(self, result: RegistrationResult) -> bool:
        try:
            from http_client import BrowserClient
        except Exception as e:
            self._log(f"Outlook Browser FSM 不可用: {e}", "warning")
            return False

        page = None
        client = None
        auth_payload: Dict[str, Any] = {}
        browser_cookies: Any = []
        try:
            if self._proxy_url_has_auth(self.proxy_url):
                self._log("Outlook Browser FSM 检测到认证代理，DrissionPage 主链暂不兼容，直接回退 HTTP 主链", "warning")
                return False

            self._log("Outlook Browser FSM 启动：优先按参考版浏览器主链注册...", "warning")
            client = self._create_root_browser_client(proxy_url=self.proxy_url, purpose="outlook-fsm")
            page = client.init_browser()

            if not self.password:
                self.password = "".join(secrets.choice(PASSWORD_CHARSET) for _ in range(DEFAULT_PASSWORD_LENGTH))

            page.get("https://chatgpt.com/")
            time.sleep(5)

            signup_btn = page.ele("text=Sign up for free", timeout=5) or page.ele("text=Sign up", timeout=3)
            if signup_btn:
                signup_btn.click()
            else:
                page.get("https://auth.openai.com/create-account")
            time.sleep(3)

            email_input = page.ele('xpath=//input[@name="email" or @id="email-address"]', timeout=30)
            if not email_input:
                self._log("Outlook Browser FSM 未定位到邮箱输入框", "warning")
                return False
            email_input.input(self.email)
            time.sleep(0.5)
            submit_btn = page.ele('xpath=//button[@type="submit" and .//text()="Continue"]', timeout=5)
            if submit_btn:
                submit_btn.click()
            else:
                page.actions.key_down("ENTER").key_up("ENTER")

            profile_submitted = False
            for _ in range(15):
                time.sleep(4)

                if page.ele("text=Your session has ended", timeout=2) or page.ele("text=Don't have an account?", timeout=2):
                    self._log("Outlook Browser FSM 捕获会话逃逸，尝试拉回注册态...", "warning")
                    signup_link = page.ele('xpath=//a[text()="Sign up"]', timeout=3)
                    if signup_link:
                        signup_link.click()
                    continue

                pwd_input = page.ele('xpath=//input[@type="password" or @name="password"]', timeout=2)
                if pwd_input:
                    self._log("Outlook Browser FSM 进入密码注入阶段")
                    self._browser_reference_smart_fill(
                        page,
                        'xpath=//input[@type="password" or @name="password"]',
                        self.password,
                        click_first=True,
                    )
                    time.sleep(1.5)
                    btn = page.ele('xpath=//button[@type="submit" and .//text()="Continue"]', timeout=4)
                    if btn:
                        btn.click()
                    else:
                        page.actions.key_down("ENTER").key_up("ENTER")
                    continue

                otp_input = page.ele('xpath=//input[@autocomplete="one-time-code" or contains(@class, "code")]', timeout=2)
                if page.ele("text=Check your inbox", timeout=2) or otp_input:
                    pwd_bypass_btn = page.ele("text=Continue with password", timeout=1)
                    if pwd_bypass_btn:
                        self._log("Outlook Browser FSM 命中密码直通按钮，优先走密码分支")
                        try:
                            pwd_bypass_btn.click()
                        except Exception:
                            page.run_js("arguments[0].click();", pwd_bypass_btn)
                        continue

                    self._log("Outlook Browser FSM 进入 OTP 捕获阶段")
                    otp_code = self.email_service.get_verification_code(email=self.inbox_email or self.email, timeout=120)
                    if otp_code:
                        self._browser_reference_smart_fill(
                            page,
                            'xpath=//input[@autocomplete="one-time-code" or contains(@class, "code")]',
                            otp_code,
                            click_first=True,
                        )
                        time.sleep(0.5)
                        page.actions.key_down("ENTER").key_up("ENTER")
                    continue

                if page.ele("text=confirm your age", timeout=2) or page.ele('xpath=//input[@name="name"]', timeout=2):
                    self._log("Outlook Browser FSM 进入档案组装阶段")
                    info = generate_random_user_info()
                    safe_year = str(secrets.choice(range(1990, 2000)))
                    safe_month = str(secrets.choice(range(1, 13))).zfill(2)
                    safe_day = str(secrets.choice(range(1, 29))).zfill(2)
                    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
                    month_abbr = month_names[int(safe_month) - 1]

                    self._browser_reference_smart_fill(
                        page,
                        'xpath=//input[@name="name" or @placeholder="Full name"]',
                        info["name"],
                        click_first=True,
                    )
                    time.sleep(0.5)

                    comboboxes = page.eles('xpath=//select | //button[@aria-haspopup="listbox"] | //button[@role="combobox"]')
                    if len(comboboxes or []) >= 3:
                        page.actions.key_down("TAB").key_up("TAB")
                        time.sleep(0.3)
                        for char in month_abbr:
                            page.actions.type(char)
                            time.sleep(0.05)
                        page.actions.key_down("TAB").key_up("TAB")
                        time.sleep(0.3)
                        for char in str(int(safe_day)):
                            page.actions.type(char)
                            time.sleep(0.05)
                        page.actions.key_down("TAB").key_up("TAB")
                        time.sleep(0.3)
                        for char in safe_year:
                            page.actions.type(char)
                            time.sleep(0.05)
                    else:
                        page.actions.key_down("TAB").key_up("TAB")
                        time.sleep(0.3)
                        fill_value = f"{safe_month}{safe_day}{safe_year}"
                        for char in fill_value:
                            page.actions.type(char)
                            time.sleep(0.15)

                    time.sleep(1.5)
                    finish_btn = page.ele("text=Finish creating account", timeout=5)
                    if finish_btn:
                        finish_btn.click()
                    else:
                        page.actions.key_down("ENTER").key_up("ENTER")
                    profile_submitted = True
                    break

            if not profile_submitted:
                self._log("Outlook Browser FSM 未完成档案提交，回退旧注册链路", "warning")
                return False

            self._log("Outlook Browser FSM 档案已提交，等待主站会话落地...")
            try:
                page.wait.url_change("auth.openai.com", timeout=40)
            except Exception:
                pass
            time.sleep(3)

            kill_list = [
                "text=Continue",
                "text=Skip Tour",
                "text=Skip",
                "text=Next",
                "text=Done",
            ]
            for _ in range(12):
                time.sleep(1.5)
                lets_go_btn = page.ele("text=Okay, let’s go", timeout=0.5)
                if lets_go_btn:
                    try:
                        lets_go_btn.click()
                    except Exception:
                        page.run_js("arguments[0].click();", lets_go_btn)
                    time.sleep(3)
                    break
                if page.ele('xpath=//textarea[@id="prompt-textarea"]', timeout=1):
                    break
                for target in kill_list:
                    btn = page.ele(target, timeout=0.5)
                    if not btn:
                        continue
                    try:
                        btn.click()
                    except Exception:
                        page.run_js("arguments[0].click();", btn)
                    break

            self._log("Outlook Browser FSM 新建后台标签页抓取 auth/session ...")
            api_tab = None
            try:
                api_tab = page.new_tab("https://chatgpt.com/api/auth/session")
                time.sleep(3)
                body_ele = api_tab.ele("tag:body")
                page_text = body_ele.text if body_ele else getattr(api_tab, "html", "")
                start_idx = str(page_text or "").find("{")
                end_idx = str(page_text or "").rfind("}") + 1
                if start_idx != -1 and end_idx > start_idx:
                    auth_payload = json.loads(str(page_text)[start_idx:end_idx])
            except Exception as e:
                self._log(f"Outlook Browser FSM auth/session 抓取异常: {e}", "warning")
            finally:
                if api_tab:
                    try:
                        api_tab.close()
                    except Exception:
                        pass

            try:
                browser_cookies = page.cookies() or []
            except Exception:
                browser_cookies = []
            self._merge_browser_cookies_into_session(browser_cookies)

            cookies_text = self._build_cookie_text_from_browser_items(browser_cookies)
            full_session_token = str(auth_payload.get("sessionToken") or "").strip()
            if not full_session_token:
                full_session_token = self._extract_session_token_from_cookie_text(cookies_text)
            access_token = str(auth_payload.get("accessToken") or auth_payload.get("access_token") or "").strip()

            account_node = auth_payload.get("account")
            if isinstance(account_node, dict):
                account_id = str(account_node.get("id") or "").strip()
                structure = str(account_node.get("structure") or "").strip().lower()
                if account_id:
                    result.account_id = result.account_id or account_id
                    self._create_account_account_id = self._create_account_account_id or account_id
                if structure == "personal" and account_id:
                    result.workspace_id = result.workspace_id or account_id
                    self._create_account_workspace_id = self._create_account_workspace_id or account_id
            user_node = auth_payload.get("user")
            if isinstance(user_node, dict):
                user_email = str(user_node.get("email") or "").strip().lower()
                if user_email:
                    result.email = user_email

            result.password = self.password or ""
            result.session_token = full_session_token or result.session_token
            result.access_token = access_token or result.access_token
            self.session_token = result.session_token or self.session_token
            self._create_account_completed = True

            if self.session is None:
                self.session = self.http_client.session
            if result.session_token:
                try:
                    self.session.cookies.set("__Secure-next-auth.session-token", result.session_token, domain=".chatgpt.com", path="/")
                except Exception:
                    pass

            self._capture_auth_session_tokens(result, access_hint=result.access_token, referer="https://chatgpt.com/")
            self._backfill_identity_from_current_session(result, source_label="Outlook Browser FSM")
            self._backfill_oauth_tokens_from_authenticated_session(result, source_label="Outlook Browser FSM")
            self._finalize_result_with_current_tokens(result, workspace_hint=result.workspace_id, source="Outlook")

            if not result.access_token:
                self._log("Outlook Browser FSM 已完成注册，但未抓到 access_token，回退旧注册链路", "warning")
                return False

            self._log(
                "Outlook Browser FSM 已建立会话: "
                f"account={'有' if bool(result.account_id) else '无'}, "
                f"workspace={'有' if bool(result.workspace_id) else '无'}, "
                f"access={'有' if bool(result.access_token) else '无'}, "
                f"refresh={'有' if bool(result.refresh_token) else '无'}",
                "warning",
            )
            return True
        except Exception as e:
            self._log(f"Outlook Browser FSM 异常，回退旧注册链路: {e}", "warning")
            return False
        finally:
            if client:
                try:
                    client.close()
                except Exception:
                    pass

    def _dump_session_cookies(self) -> str:
        """导出当前会话 cookies（用于后续支付/绑卡自动化）。"""
        if not self.session:
            return ""
        try:
            cookie_map: dict[str, str] = {}
            order: list[str] = []

            def _push(name: Optional[str], value: Optional[str]):
                key = str(name or "").strip()
                val = str(value or "").strip()
                if not key:
                    return
                if key not in cookie_map:
                    cookie_map[key] = val
                    order.append(key)
                    return
                # 同名 cookie 可能来自不同域/路径：优先保留非空且更长值，避免空值覆盖有效分片。
                prev = str(cookie_map.get(key) or "").strip()
                if (not prev and val) or (val and len(val) > len(prev)):
                    cookie_map[key] = val

            # 1) 常规 requests/curl_cffi 字典接口
            try:
                for key, value in self.session.cookies.items():
                    _push(key, value)
            except Exception:
                pass

            # 2) CookieJar 接口（可拿到分片 cookie）
            try:
                jar = getattr(self.session.cookies, "jar", None)
                if jar is not None:
                    for cookie in jar:
                        _push(getattr(cookie, "name", ""), getattr(cookie, "value", ""))
            except Exception:
                pass

            # 3) 关键 cookie 兜底读取
            for key in (
                "oai-did",
                "oai-client-auth-session",
                "__Secure-next-auth.session-token",
                "_Secure-next-auth.session-token",
            ):
                try:
                    _push(key, self.session.cookies.get(key))
                except Exception:
                    continue

            pairs = [(k, cookie_map.get(k, "")) for k in order if k]
            return "; ".join(f"{k}={v}" for k, v in pairs if k)
        except Exception:
            return ""

    @staticmethod
    def _extract_session_token_from_cookie_jar(cookie_jar) -> str:
        """
        从 CookieJar 中提取 next-auth session token（兼容分片 + 重复域名）。
        """
        if not cookie_jar:
            return ""

        entries: list[tuple[str, str]] = []
        try:
            for key, value in cookie_jar.items():
                entries.append((str(key or "").strip(), str(value or "").strip()))
        except Exception:
            pass

        try:
            jar = getattr(cookie_jar, "jar", None)
            if jar is not None:
                for cookie in jar:
                    entries.append(
                        (
                            str(getattr(cookie, "name", "") or "").strip(),
                            str(getattr(cookie, "value", "") or "").strip(),
                        )
                    )
        except Exception:
            pass

        direct_candidates = [
            val
            for name, val in entries
            if name in ("__Secure-next-auth.session-token", "_Secure-next-auth.session-token") and val
        ]
        if direct_candidates:
            return max(direct_candidates, key=len)

        chunk_map: dict[int, str] = {}
        for name, value in entries:
            if not (
                name.startswith("__Secure-next-auth.session-token.")
                or name.startswith("_Secure-next-auth.session-token.")
            ):
                continue
            if not value:
                continue
            try:
                idx = int(name.rsplit(".", 1)[-1])
            except Exception:
                continue
            prev = chunk_map.get(idx, "")
            if not prev or len(value) > len(prev):
                chunk_map[idx] = value

        if chunk_map:
            return "".join(chunk_map[i] for i in sorted(chunk_map.keys()))
        return ""

    @staticmethod
    def _flatten_set_cookie_headers(response) -> str:
        """
        合并多条 Set-Cookie（包含分片 cookie）。
        """
        try:
            headers = getattr(response, "headers", None)
            if headers is None:
                return ""
            if hasattr(headers, "get_list"):
                values = headers.get_list("set-cookie")
                if values:
                    return " | ".join(str(v or "") for v in values if v is not None)
            if hasattr(headers, "get_all"):
                values = headers.get_all("set-cookie")
                if values:
                    return " | ".join(str(v or "") for v in values if v is not None)
            return str(headers.get("set-cookie") or "")
        except Exception:
            return ""

    @staticmethod
    def _extract_request_cookie_header(response) -> str:
        """
        从响应对象关联的请求头中提取 Cookie。
        对齐 F12 Network -> Request Headers -> Cookie 的观测路径。
        """
        try:
            request_obj = getattr(response, "request", None)
            if request_obj is None:
                return ""
            headers = getattr(request_obj, "headers", None)
            if headers is None:
                return ""

            if hasattr(headers, "get"):
                value = headers.get("cookie") or headers.get("Cookie")
                if value:
                    return str(value)

            try:
                for key, value in dict(headers).items():
                    if str(key or "").strip().lower() == "cookie" and value:
                        return str(value)
            except Exception:
                pass
        except Exception:
            pass
        return ""

    def _generate_password(self, length: int = DEFAULT_PASSWORD_LENGTH) -> str:
        """生成随机密码"""
        return ''.join(secrets.choice(PASSWORD_CHARSET) for _ in range(length))

    def _check_ip_location(self) -> Tuple[bool, Optional[str]]:
        """检查 IP 地理位置"""
        try:
            return self.http_client.check_ip_location()
        except Exception as e:
            self._log(f"检查 IP 地理位置失败: {e}", "error")
            return False, None

    def _create_email(self) -> bool:
        """创建邮箱"""
        try:
            self._log(f"正在创建 {self.email_service.service_type.value} 邮箱，先给新账号整个收件箱...")
            self.email_info = self.email_service.create_email()

            if not self.email_info or "email" not in self.email_info:
                self._log("创建邮箱失败: 返回信息不完整", "error")
                return False

            raw_email = str(self.email_info["email"] or "").strip()
            normalized_email = raw_email.lower()

            # 保留原始收件地址，注册链路统一使用规范化邮箱，规避 "Failed to register username"。
            self.inbox_email = raw_email
            self.email = normalized_email
            self.email_info["email"] = normalized_email

            if raw_email and raw_email != normalized_email:
                self._log(f"邮箱规范化: {raw_email} -> {normalized_email}")

            self._log(f"邮箱已就位，地址新鲜出炉: {self.email}")
            return True

        except Exception as e:
            self._log(f"创建邮箱失败: {e}", "error")
            return False

    def _build_outlook_recovery_payload(self) -> Dict[str, str]:
        """提取当前 Outlook 账号的恢复信息，冗余保存到账号表。"""
        if str(self.email_service.service_type.value or "").strip().lower() != "outlook":
            return {}

        target_email = str(
            (self.email_info or {}).get("email") or self.email or ""
        ).strip().lower()
        raw_config = getattr(self.email_service, "config", None)
        config = raw_config if isinstance(raw_config, dict) else {}

        candidates: List[Dict[str, Any]] = []
        if "email" in config and ("password" in config or "refresh_token" in config):
            candidates.append(config)
        for item in config.get("accounts", []) if isinstance(config.get("accounts"), list) else []:
            if isinstance(item, dict):
                candidates.append(item)

        selected: Dict[str, Any] = {}
        for item in candidates:
            item_email = str(item.get("email") or "").strip().lower()
            if target_email and item_email == target_email:
                selected = item
                break
        if not selected and len(candidates) == 1:
            selected = candidates[0]

        payload = {
            "email": str(selected.get("email") or target_email or "").strip(),
            "password": str(selected.get("password") or "").strip(),
            "client_id": str(selected.get("client_id") or "").strip(),
            "refresh_token": str(selected.get("refresh_token") or "").strip(),
        }
        return payload if any(payload.values()) else {}

    def _start_oauth(self) -> bool:
        """开始 OAuth 流程"""
        try:
            self._log("开始 OAuth 授权流程，去门口刷个脸...")
            self.oauth_start = self.oauth_manager.start_oauth()
            self._log(f"OAuth URL 已备好，通道已经打开: {self.oauth_start.auth_url[:80]}...")
            return True
        except Exception as e:
            self._log(f"生成 OAuth URL 失败: {e}", "error")
            return False

    def _init_session(self) -> bool:
        """初始化会话"""
        try:
            self.session = self.http_client.session
            return True
        except Exception as e:
            self._log(f"初始化会话失败: {e}", "error")
            return False

    def _get_device_id(self) -> Optional[str]:
        """获取 Device ID"""
        if not self.oauth_start:
            return None

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                if not self.session:
                    self.session = self.http_client.session

                response = self.session.get(
                    self.oauth_start.auth_url,
                    timeout=20
                )
                did = self.session.cookies.get("oai-did")

                if not did:
                    # 对齐 ABCard：部分环境 cookie 不落盘，尝试从 HTML 文本提取
                    try:
                        m = re.search(r'oai-did["\s:=]+([a-f0-9-]{36})', str(response.text or ""), re.IGNORECASE)
                        if m:
                            did = str(m.group(1) or "").strip()
                            if did:
                                try:
                                    self.session.cookies.set("oai-did", did, domain=".chatgpt.com", path="/")
                                except Exception:
                                    pass
                    except Exception:
                        pass

                if did:
                    self._log(f"Device ID: {did}")
                    return did

                self._log(
                    f"获取 Device ID 失败: 未返回 oai-did Cookie (HTTP {response.status_code}, 第 {attempt}/{max_attempts} 次)",
                    "warning" if attempt < max_attempts else "error"
                )
            except Exception as e:
                self._log(
                    f"获取 Device ID 失败: {e} (第 {attempt}/{max_attempts} 次)",
                    "warning" if attempt < max_attempts else "error"
                )

            if attempt < max_attempts:
                time.sleep(attempt)
                self.http_client.close()
                self.session = self.http_client.session

        # 对齐 ABCard：无法从响应拿到 did 时，优先复用上次成功 did，再使用 UUID 兜底。
        fallback_did = str(self.device_id or "").strip() or str(uuid.uuid4())
        try:
            if self.session:
                self.session.cookies.set("oai-did", fallback_did, domain=".chatgpt.com", path="/")
        except Exception:
            pass
        self._log(f"未获取到 oai-did，使用兜底 Device ID: {fallback_did}", "warning")
        return fallback_did

    def _check_sentinel(self, did: str) -> Optional[str]:
        """检查 Sentinel 拦截"""
        try:
            sen_token = self.http_client.check_sentinel(did)
            if sen_token:
                self._log(f"Sentinel token 获取成功")
                return sen_token
            self._log("Sentinel 检查失败: 未获取到 token", "warning")
            return None

        except Exception as e:
            self._log(f"Sentinel 检查异常: {e}", "warning")
            return None

    def _submit_auth_start(
        self,
        did: str,
        sen_token: Optional[str],
        *,
        screen_hint: str,
        referer: str,
        log_label: str,
        record_existing_account: bool = True,
    ) -> SignupFormResult:
        """
        提交授权入口表单

        Returns:
            SignupFormResult: 提交结果，包含账号状态判断
        """
        max_attempts = 3
        current_did = str(did or "").strip()
        current_sen_token = str(sen_token or "").strip() if sen_token else None
        for attempt in range(1, max_attempts + 1):
            try:
                request_body = json.dumps({
                    "username": {
                        "value": self.email,
                        "kind": "email",
                    },
                    "screen_hint": screen_hint,
                })

                headers = {
                    "referer": referer,
                    "accept": "application/json",
                    "content-type": "application/json",
                }

                if current_sen_token:
                    sentinel = json.dumps({
                        "p": "",
                        "t": "",
                        "c": current_sen_token,
                        "id": current_did,
                        "flow": "authorize_continue",
                    })
                    headers["openai-sentinel-token"] = sentinel

                response = self.session.post(
                    OPENAI_API_ENDPOINTS["signup"],
                    headers=headers,
                    data=request_body,
                )

                self._log(f"{log_label}状态: {response.status_code}")

                if response.status_code == 429 and attempt < max_attempts:
                    wait_seconds = min(18, 5 * attempt)
                    self._log(
                        f"{log_label}命中限流 429（第 {attempt}/{max_attempts} 次），{wait_seconds}s 后自动重试...",
                        "warning",
                    )
                    time.sleep(wait_seconds)
                    continue

                # 部分网络/会话边界情况下会返回 409，做自愈重试而非直接失败。
                if response.status_code == 409 and attempt < max_attempts:
                    wait_seconds = min(10, 2 * attempt)
                    self._log(
                        f"{log_label}命中 409（第 {attempt}/{max_attempts} 次），"
                        f"会话上下文可能冲突，{wait_seconds}s 后自动重试...",
                        "warning",
                    )
                    # 尝试刷新 sentinel，避免 token 过期导致冲突。
                    try:
                        refreshed = self._check_sentinel(current_did)
                        if refreshed:
                            current_sen_token = refreshed
                    except Exception:
                        pass
                    # 预热一次授权页，帮助服务端重建登录上下文。
                    try:
                        if self.oauth_start and getattr(self.oauth_start, "auth_url", None):
                            self.session.get(str(self.oauth_start.auth_url), timeout=12)
                    except Exception:
                        pass
                    time.sleep(wait_seconds)
                    continue

                if response.status_code != 200:
                    return SignupFormResult(
                        success=False,
                        error_message=f"HTTP {response.status_code}: {response.text[:200]}"
                    )

                # 解析响应判断账号状态
                try:
                    response_data = response.json()
                    page_type = response_data.get("page", {}).get("type", "")
                    self._log(f"响应页面类型: {page_type}")

                    is_existing = self._is_email_otp_page_type(page_type)

                    if is_existing:
                        self._otp_sent_at = time.time()
                        otp_start_url = self._extract_continue_url_candidate(response_data)
                        if otp_start_url:
                            self._last_email_otp_start_url = otp_start_url
                            self._log(f"{log_label}返回 OTP start_url: {otp_start_url[:100]}...")
                        if record_existing_account:
                            self._log(f"检测到已注册账号，将自动切换到登录流程")
                            self._is_existing_account = True
                        else:
                            self._log("登录流程已触发，等待系统自动发送的验证码")

                    return SignupFormResult(
                        success=True,
                        page_type=page_type,
                        is_existing_account=is_existing,
                        response_data=response_data
                    )

                except Exception as parse_error:
                    self._log(f"解析响应失败: {parse_error}", "warning")
                    # 无法解析，默认成功
                    return SignupFormResult(success=True)

            except Exception as e:
                if attempt < max_attempts:
                    self._log(
                        f"{log_label}异常（第 {attempt}/{max_attempts} 次）: {e}，准备重试...",
                        "warning",
                    )
                    time.sleep(2 * attempt)
                    continue
                self._log(f"{log_label}失败: {e}", "error")
                return SignupFormResult(success=False, error_message=str(e))

        return SignupFormResult(success=False, error_message=f"{log_label}失败: 超过最大重试次数")

    def _submit_signup_form(
        self,
        did: str,
        sen_token: Optional[str],
        *,
        record_existing_account: bool = True,
    ) -> SignupFormResult:
        """提交注册入口表单。"""
        return self._submit_auth_start(
            did,
            sen_token,
            screen_hint="signup",
            referer="https://auth.openai.com/create-account",
            log_label="提交注册表单",
            record_existing_account=record_existing_account,
        )

    def _submit_login_start(self, did: str, sen_token: Optional[str]) -> SignupFormResult:
        """提交登录入口表单。"""
        return self._submit_auth_start(
            did,
            sen_token,
            screen_hint="login",
            referer="https://auth.openai.com/log-in",
            log_label="提交登录入口",
            record_existing_account=False,
        )

    def _submit_login_password(self) -> SignupFormResult:
        """提交登录密码，进入邮箱验证码页面。"""
        max_attempts = 3
        password_text = str(self.password or "").strip()
        if not password_text and self.email:
            try:
                with get_db() as db:
                    account = crud.get_account_by_email(db, self.email)
                    db_password = str(getattr(account, "password", "") or "").strip() if account else ""
                    if db_password:
                        self.password = db_password
                        password_text = db_password
                        self._log("登录阶段未发现内存密码，已从账号库回填密码")
            except Exception as e:
                self._log(f"登录阶段尝试回填密码失败: {e}", "warning")

        if not password_text:
            return SignupFormResult(
                success=False,
                error_message="登录密码为空：该邮箱可能是已存在账号但当前任务未持有密码",
            )

        for attempt in range(1, max_attempts + 1):
            try:
                response = self.session.post(
                    OPENAI_API_ENDPOINTS["password_verify"],
                    headers={
                        "referer": "https://auth.openai.com/log-in/password",
                        "accept": "application/json",
                        "content-type": "application/json",
                    },
                    data=json.dumps({"password": self.password}),
                )

                self._log(f"提交登录密码状态: {response.status_code}")

                if response.status_code == 429 and attempt < max_attempts:
                    wait_seconds = min(18, 5 * attempt)
                    self._log(
                        f"提交登录密码命中限流 429（第 {attempt}/{max_attempts} 次），{wait_seconds}s 后自动重试...",
                        "warning",
                    )
                    time.sleep(wait_seconds)
                    continue

                if response.status_code == 401 and attempt < max_attempts:
                    body = str(response.text or "")
                    if "invalid_username_or_password" in body:
                        wait_seconds = min(12, 3 * attempt)
                        self._log(
                            f"提交登录密码命中 401（第 {attempt}/{max_attempts} 次），"
                            f"疑似密码尚未生效或历史账号密码不一致，{wait_seconds}s 后自动重试...",
                            "warning",
                        )
                        time.sleep(wait_seconds)
                        continue

                if response.status_code != 200:
                    return SignupFormResult(
                        success=False,
                        error_message=f"HTTP {response.status_code}: {response.text[:200]}"
                    )

                response_data = response.json()
                page_type = response_data.get("page", {}).get("type", "")
                self._log(f"登录密码响应页面类型: {page_type}")

                is_existing = self._is_email_otp_page_type(page_type)
                if is_existing:
                    self._otp_sent_at = time.time()
                    otp_start_url = self._extract_continue_url_candidate(response_data)
                    if otp_start_url:
                        self._last_email_otp_start_url = otp_start_url
                        self._log(f"登录密码返回 OTP start_url: {otp_start_url[:100]}...")
                    self._log("登录密码校验通过，等待系统自动发送的验证码")

                return SignupFormResult(
                    success=True,
                    page_type=page_type,
                    is_existing_account=is_existing,
                    response_data=response_data,
                )

            except Exception as e:
                if attempt < max_attempts:
                    self._log(
                        f"提交登录密码异常（第 {attempt}/{max_attempts} 次）: {e}，准备重试...",
                        "warning",
                    )
                    time.sleep(2 * attempt)
                    continue
                self._log(f"提交登录密码失败: {e}", "error")
                return SignupFormResult(success=False, error_message=str(e))

        return SignupFormResult(success=False, error_message="提交登录密码失败: 超过最大重试次数")

    def _reset_auth_flow(self) -> None:
        """重置会话，准备重新发起 OAuth 流程。"""
        self.http_client.close()
        self.session = None
        self.oauth_start = None
        self.session_token = None
        self._otp_sent_at = None

    def _prepare_authorize_flow(self, label: str) -> Tuple[Optional[str], Optional[str]]:
        """初始化当前阶段的授权流程，返回 device id 和 sentinel token。"""
        self._log(f"{label}: 先把会话热热身...")
        if not self._init_session():
            return None, None

        self._log(f"{label}: OAuth 流程准备开跑，系好鞋带...")
        if not self._start_oauth():
            return None, None

        self._log(f"{label}: 领取 Device ID 通行证...")
        did = str(self._get_device_id() or "").strip()
        if not did:
            return None, None

        self.device_id = did

        self._log(f"{label}: 解一道 Sentinel POW 小题，答对才给进...")
        sen_token = self._check_sentinel(did)
        if not sen_token:
            return did, None

        self._log(f"{label}: Sentinel 点头放行，继续前进")
        return did, sen_token

    @staticmethod
    def _extract_session_token_from_cookie_text(cookie_text: str) -> str:
        """从 Cookie 文本中提取 next-auth session token（兼容分片）。"""
        text = str(cookie_text or "")
        if not text:
            return ""

        direct = re.search(r"(?:^|[;,]\s*)(?:__|_)Secure-next-auth\.session-token=([^;,]*)", text)
        if direct:
            direct_val = str(direct.group(1) or "").strip().strip('"').strip("'")
            if direct_val:
                return direct_val

        parts = re.findall(r"(?:__|_)Secure-next-auth\.session-token\.(\d+)=([^;,]*)", text)
        if not parts:
            return ""

        chunk_map = {}
        for idx, value in parts:
            try:
                clean_value = str(value or "").strip().strip('"').strip("'")
                if clean_value:
                    chunk_map[int(idx)] = clean_value
            except Exception:
                continue
        if not chunk_map:
            return ""
        return "".join(chunk_map[i] for i in sorted(chunk_map.keys()))

    def _warmup_chatgpt_session(self) -> None:
        """
        仅预热 chatgpt 首页，避免提前消费一次性 continue_url。
        """
        try:
            self.session.get(
                "https://chatgpt.com/",
                headers={
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "referer": "https://auth.openai.com/",
                    "user-agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                    ),
                },
                timeout=20,
            )
        except Exception as e:
            self._log(f"chatgpt 首页预热异常: {e}", "warning")

    @staticmethod
    def _is_registration_gate_url(url: str) -> bool:
        candidate = str(url or "").strip().lower()
        if not candidate:
            return False
        return ("auth.openai.com/about-you" in candidate) or ("auth.openai.com/add-phone" in candidate)

    @staticmethod
    def _is_oauth_callback_url(url: str) -> bool:
        candidate = str(url or "").strip()
        if not candidate:
            return False
        try:
            parsed = urllib.parse.urlparse(candidate)
            path = (parsed.path or "").lower()
            if ("/auth/callback" not in path) and ("/api/auth/callback/openai" not in path):
                return False
            query = urllib.parse.parse_qs(parsed.query or "", keep_blank_values=True)
            return bool(query.get("code") or query.get("error"))
        except Exception:
            return False

    def _is_local_oauth_callback_url(self, url: str) -> bool:
        candidate = str(url or "").strip()
        if not candidate:
            return False
        try:
            parsed = urllib.parse.urlparse(candidate)
            redirect_parsed = urllib.parse.urlparse(str(get_settings().openai_redirect_uri or "").strip())
            if (parsed.path or "").lower() != (redirect_parsed.path or "").lower():
                return False
            if redirect_parsed.netloc and parsed.netloc and parsed.netloc.lower() != redirect_parsed.netloc.lower():
                return False
            query = urllib.parse.parse_qs(parsed.query or "", keep_blank_values=True)
            return bool(query.get("code") or query.get("error"))
        except Exception:
            return False

    @staticmethod
    def _find_first_string_value(payload: Any, keys: tuple[str, ...]) -> str:
        lowered_keys = {str(key or "").strip().lower() for key in keys if str(key or "").strip()}

        def _visit(node: Any) -> str:
            if isinstance(node, dict):
                for key, value in node.items():
                    key_text = str(key or "").strip().lower()
                    if key_text in lowered_keys and isinstance(value, str) and value.strip():
                        return value.strip()
                    if isinstance(value, (dict, list, tuple)):
                        found = _visit(value)
                        if found:
                            return found
            elif isinstance(node, (list, tuple)):
                for item in node:
                    found = _visit(item)
                    if found:
                        return found
            return ""

        return _visit(payload)

    @staticmethod
    def _is_email_otp_page_type(page_type: str) -> bool:
        candidate = str(page_type or "").strip().lower()
        return candidate in {
            str(OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]).strip().lower(),
            "email-verification",
            "email_otp",
            "email-otp",
        }

    def _extract_continue_url_candidate(self, payload: Any) -> str:
        candidate = self._find_first_string_value(
            payload,
            (
                "continue_url",
                "continueUrl",
                "next_url",
                "nextUrl",
                "redirect_url",
                "redirectUrl",
                "url",
            ),
        )
        candidate = str(candidate or "").strip()
        if candidate.startswith("/"):
            try:
                candidate = urllib.parse.urljoin(OPENAI_API_ENDPOINTS["validate_otp"], candidate)
            except Exception:
                pass
        return candidate

    @staticmethod
    def _is_login_password_page_type(page_type: str) -> bool:
        candidate = str(page_type or "").strip().lower()
        return candidate == str(OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]).strip().lower()

    @staticmethod
    def _is_password_registration_page_type(page_type: str) -> bool:
        candidate = str(page_type or "").strip().lower()
        return candidate == str(OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]).strip().lower()

    @staticmethod
    def _is_add_phone_page_type(page_type: str) -> bool:
        candidate = str(page_type or "").strip().lower()
        return candidate in {"add_phone", "add-phone", "addphone"}

    def _refresh_auth_cookie_workspace_diagnostics(self, *, source_label: str) -> None:
        raw_cookie = ""
        try:
            raw_cookie = str(self.session.cookies.get("oai-client-auth-session") or "").strip() if self.session else ""
        except Exception:
            raw_cookie = ""

        workspace_id = ""
        if raw_cookie:
            try:
                workspace_id = str(self._extract_workspace_id_from_cookie_value(raw_cookie) or "").strip()
            except Exception as e:
                self._log(f"{source_label} 解析 oai-client-auth-session 失败: {e}", "warning")

        self._last_auth_cookie_workspace_id = workspace_id
        self._last_auth_cookie_has_workspace = bool(workspace_id)

        if not raw_cookie:
            self._log(f"{source_label} 未发现 oai-client-auth-session cookie", "warning")
            return

        if workspace_id:
            self._log(f"{source_label} auth cookie 已含 Workspace ID: {workspace_id}")
        else:
            self._log(f"{source_label} auth cookie 暂无 workspaces/workspace_id", "warning")

    def _continue_url_priority(self, url: str) -> int:
        candidate = str(url or "").strip()
        if not candidate:
            return 0
        if self._is_oauth_callback_url(candidate):
            return 30
        if self._is_registration_gate_url(candidate):
            return 10
        return 20

    def _pick_preferred_continue_url(self, *candidates: Optional[str]) -> str:
        ranked: List[Tuple[int, int, str]] = []
        for idx, raw in enumerate(candidates):
            candidate = str(raw or "").strip()
            if not candidate:
                continue
            ranked.append((self._continue_url_priority(candidate), -idx, candidate))
        if not ranked:
            return ""
        ranked.sort(reverse=True)
        return ranked[0][2]

    def _pick_browser_observed_continue_url(self, payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""

        candidates: List[str] = []
        seen: set[str] = set()
        for key in ("observed_callback_urls", "observed_urls"):
            raw_value = payload.get(key)
            values: List[str] = []
            if isinstance(raw_value, str):
                values = [raw_value]
            elif isinstance(raw_value, (list, tuple, set)):
                values = [str(item or "") for item in raw_value]

            for raw_url in values:
                candidate = str(raw_url or "").strip()
                if not candidate or candidate in seen:
                    continue
                if not (
                    self._is_oauth_callback_url(candidate)
                    or self._is_registration_gate_url(candidate)
                    or ("sign-in-with-chatgpt/codex/consent" in candidate)
                    or ("auth.openai.com/about-you" in candidate)
                ):
                    continue
                seen.add(candidate)
                candidates.append(candidate)

        return self._pick_preferred_continue_url(*candidates)

    def _should_try_direct_continue_before_browser(
        self,
        continue_url: Optional[str],
        *,
        workspace_continue: Optional[str] = None,
    ) -> bool:
        candidate = str(continue_url or "").strip()
        if not candidate:
            return False
        if self._is_registration_gate_url(candidate):
            return False
        if self._is_oauth_callback_url(candidate):
            return True

        lowered = candidate.lower()
        if any(marker in lowered for marker in ("/api/oauth/oauth2/auth", "/oauth/authorize")):
            return True

        workspace_candidate = str(workspace_continue or "").strip()
        if workspace_candidate and candidate == workspace_candidate:
            return "sign-in-with-chatgpt/codex/consent" in lowered

        return False

    @staticmethod
    def _looks_like_jwt_token(value: str) -> bool:
        raw = str(value or "").strip()
        if raw.count(".") != 2:
            return False
        return all(bool(part) for part in raw.split("."))

    def _extract_tokens_from_auth_session_payload(self, payload: Any) -> Dict[str, str]:
        token_info = {
            "access_token": "",
            "refresh_token": "",
            "id_token": "",
        }

        def _visit(node: Any) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    key_text = str(key or "").strip().lower()
                    if isinstance(value, str):
                        candidate = value.strip()
                        if not candidate:
                            continue
                        if key_text in {"accesstoken", "access_token"}:
                            token_info["access_token"] = token_info["access_token"] or candidate
                        elif key_text in {"refreshtoken", "refresh_token"}:
                            token_info["refresh_token"] = token_info["refresh_token"] or candidate
                        elif key_text in {"idtoken", "id_token"}:
                            token_info["id_token"] = token_info["id_token"] or candidate
                        elif ("token" in key_text) and self._looks_like_jwt_token(candidate):
                            if not token_info["access_token"]:
                                token_info["access_token"] = candidate
                            elif not token_info["id_token"] and candidate != token_info["access_token"]:
                                token_info["id_token"] = candidate
                    elif isinstance(value, (dict, list, tuple)):
                        _visit(value)
            elif isinstance(node, (list, tuple)):
                for item in node:
                    _visit(item)

        _visit(payload)
        return token_info

    @classmethod
    def _extract_workspace_id_from_cookie_value(cls, raw_value: str) -> str:
        candidates: List[str] = []
        seen: set[str] = set()

        def _push(value: str) -> None:
            text = str(value or "").strip()
            if not text or text in seen:
                return
            seen.add(text)
            candidates.append(text)

        current = str(raw_value or "").strip()
        if not current:
            return ""
        _push(current)

        decoded = current
        for _ in range(2):
            unquoted = urllib.parse.unquote(decoded)
            if unquoted == decoded:
                break
            _push(unquoted)
            decoded = unquoted

        if "." in current:
            for segment in current.split("."):
                _push(segment)

        while candidates:
            candidate = candidates.pop(0).strip()
            if not candidate:
                continue

            if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in {"\"", "'"}:
                _push(candidate[1:-1])

            try:
                payload = json.loads(candidate)
            except Exception:
                payload = None

            if payload is not None:
                workspace_id = cls._extract_workspace_id_from_payload(payload)
                if workspace_id:
                    return workspace_id
                if isinstance(payload, str):
                    _push(payload)

            try:
                pad = "=" * ((4 - (len(candidate) % 4)) % 4)
                decoded_text = base64.urlsafe_b64decode((candidate + pad).encode("ascii")).decode("utf-8")
            except Exception:
                decoded_text = ""
            if decoded_text and decoded_text != candidate:
                _push(decoded_text)

        return ""

    @classmethod
    def _extract_workspace_id_from_text(cls, raw_text: str) -> str:
        text = str(raw_text or "")
        if not text:
            return ""

        variants: List[str] = []
        seen: set[str] = set()

        def _push(value: str) -> None:
            candidate = str(value or "")
            if not candidate or candidate in seen:
                return
            seen.add(candidate)
            variants.append(candidate)

        _push(text)
        decoded = text
        for _ in range(2):
            unquoted = urllib.parse.unquote(decoded)
            if unquoted == decoded:
                break
            _push(unquoted)
            decoded = unquoted
        _push(html.unescape(decoded))
        _push(decoded.replace("\\/", "/").replace("\\u002F", "/").replace("\\u0022", "\""))

        patterns = (
            r'"workspace_id"\s*:\s*"([^"]+)"',
            r'"workspaceId"\s*:\s*"([^"]+)"',
            r'"current_workspace_id"\s*:\s*"([^"]+)"',
            r'"currentWorkspaceId"\s*:\s*"([^"]+)"',
            r'"active_workspace_id"\s*:\s*"([^"]+)"',
            r'"activeWorkspaceId"\s*:\s*"([^"]+)"',
            r'"selected_workspace_id"\s*:\s*"([^"]+)"',
            r'"selectedWorkspaceId"\s*:\s*"([^"]+)"',
            r'"default_workspace_id"\s*:\s*"([^"]+)"',
            r'"defaultWorkspaceId"\s*:\s*"([^"]+)"',
            r'"workspace"\s*:\s*\{[\s\S]{0,240}?"id"\s*:\s*"([^"]+)"',
            r'"workspaces"\s*:\s*\[[\s\S]{0,240}?"id"\s*:\s*"([^"]+)"',
            r'workspace_id\s*:\s*"([^"]+)"',
            r'workspaceId\s*:\s*"([^"]+)"',
        )

        for variant in variants:
            workspace_id = cls._extract_workspace_id_from_cookie_value(variant)
            if workspace_id:
                return workspace_id
            for pattern in patterns:
                match = re.search(pattern, variant, re.IGNORECASE)
                if match:
                    workspace_id = str(match.group(1) or "").strip()
                    if workspace_id:
                        return workspace_id

            for script_match in re.finditer(r"<script[^>]*>([\s\S]*?)</script>", variant, re.IGNORECASE):
                script_body = str(script_match.group(1) or "").strip()
                if not script_body:
                    continue
                workspace_id = cls._extract_workspace_id_from_cookie_value(script_body)
                if workspace_id:
                    return workspace_id

        return ""

    def _consume_oauth_callback_for_session(
        self,
        result: RegistrationResult,
        callback_url: str,
        *,
        stage_label: str,
        referer: Optional[str] = None,
    ) -> bool:
        callback = str(callback_url or "").strip()
        if not self._is_oauth_callback_url(callback):
            return False

        final_url = callback
        try:
            response = self.session.get(
                callback,
                headers={
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "referer": str(referer or "https://chatgpt.com/auth/login").strip(),
                    "user-agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                    ),
                },
                allow_redirects=True,
                timeout=25,
            )
            final_url = str(getattr(response, "url", "") or callback).strip()
        except Exception as e:
            self._log(f"{stage_label} callback 补跳异常: {e}", "warning")

        if self._capture_auth_session_tokens(
            result,
            access_hint=result.access_token,
            referer=final_url or callback,
        ):
            self._log(f"{stage_label} callback 已建立 ChatGPT session")
            return True
        return False

    def _merge_token_info_into_result(self, result: RegistrationResult, token_info: Optional[Dict[str, Any]]) -> None:
        if not isinstance(token_info, dict):
            return
        result.account_id = str(token_info.get("account_id") or result.account_id or "").strip()
        result.access_token = str(token_info.get("access_token") or result.access_token or "").strip()
        result.refresh_token = str(token_info.get("refresh_token") or result.refresh_token or "").strip()
        result.id_token = str(token_info.get("id_token") or result.id_token or "").strip()

    @staticmethod
    def _extract_workspace_id_from_payload(payload: Any) -> str:
        def _visit(node: Any) -> str:
            if isinstance(node, dict):
                workspace_id = str(
                    node.get("workspace_id")
                    or node.get("workspaceId")
                    or node.get("current_workspace_id")
                    or node.get("currentWorkspaceId")
                    or node.get("active_workspace_id")
                    or node.get("activeWorkspaceId")
                    or node.get("selected_workspace_id")
                    or node.get("selectedWorkspaceId")
                    or node.get("default_workspace_id")
                    or node.get("defaultWorkspaceId")
                    or ""
                ).strip()
                if workspace_id:
                    return workspace_id

                workspace = node.get("workspace")
                if isinstance(workspace, dict):
                    workspace_id = str(
                        workspace.get("id")
                        or workspace.get("workspace_id")
                        or workspace.get("workspaceId")
                        or ""
                    ).strip()
                    if workspace_id:
                        return workspace_id

                workspaces = node.get("workspaces")
                if isinstance(workspaces, list):
                    for item in workspaces:
                        if isinstance(item, dict):
                            workspace_id = str(item.get("id") or "").strip()
                            if workspace_id:
                                return workspace_id
                        found = _visit(item)
                        if found:
                            return found

                for value in node.values():
                    if isinstance(value, (dict, list, tuple)):
                        found = _visit(value)
                        if found:
                            return found

            elif isinstance(node, (list, tuple)):
                for item in node:
                    found = _visit(item)
                    if found:
                        return found

            return ""

        return _visit(payload)

    def _fetch_chatgpt_me_payload(self, access_hint: Optional[str] = None) -> Any:
        headers = {
            "accept": "application/json",
            "referer": "https://chatgpt.com/",
            "origin": "https://chatgpt.com",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        }
        access_token = str(access_hint or "").strip()
        if access_token:
            headers["authorization"] = f"Bearer {access_token}"

        try:
            response = self.session.get(
                "https://chatgpt.com/backend-api/me",
                headers=headers,
                timeout=20,
            )
            if response.status_code != 200:
                self._log(f"backend-api/me 返回异常状态: {response.status_code}", "warning")
                return None
            return response.json() or {}
        except Exception as e:
            self._log(f"获取 backend-api/me 失败: {e}", "warning")
            return None

    def _fetch_client_auth_session_dump(self) -> Any:
        consent_url = "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"
        consent_workspace = ""
        consent_referer = consent_url
        try:
            consent_response = self.session.get(
                consent_url,
                headers={
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "referer": "https://auth.openai.com/add-phone",
                    "user-agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                    ),
                },
                timeout=20,
            )
            consent_referer = str(getattr(consent_response, "url", "") or consent_url).strip() or consent_url
            consent_workspace = self._extract_workspace_id_from_text(getattr(consent_response, "text", ""))
            if consent_workspace:
                self._log(f"consent 页内提取到 Workspace ID: {consent_workspace}")
        except Exception as e:
            self._log(f"预热 consent 页失败: {e}", "warning")

        try:
            response = self.session.get(
                "https://auth.openai.com/api/accounts/client_auth_session_dump",
                headers={
                    "accept": "application/json",
                    "referer": consent_referer,
                    "user-agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                    ),
                },
                timeout=20,
            )
            if response.status_code != 200:
                self._log(f"client_auth_session_dump 返回异常状态: {response.status_code}", "warning")
                return None
            payload = response.json() or {}
            workspace_id = self._extract_workspace_id_from_payload(payload)
            if workspace_id:
                return payload
            if consent_workspace:
                self._log("client_auth_session_dump 未直接返回 workspace，改用 consent 页里的 workspace_id", "warning")
                return {"client_auth_session": {"workspace_id": consent_workspace}}
            if isinstance(payload, dict):
                top_keys = ",".join(sorted(str(key) for key in payload.keys())[:8])
                session_payload = payload.get("client_auth_session") or payload.get("session") or {}
                session_keys = ""
                if isinstance(session_payload, dict):
                    session_keys = ",".join(sorted(str(key) for key in session_payload.keys())[:8])
                self._log(
                    f"client_auth_session_dump 未返回 workspace（top_keys={top_keys or 'none'}, session_keys={session_keys or 'none'}）",
                    "warning",
                )
            return payload
        except Exception as e:
            self._log(f"获取 client_auth_session_dump 失败: {e}", "warning")
            return None

    def _backfill_identity_from_current_session(
        self,
        result: RegistrationResult,
        *,
        source_label: str,
    ) -> bool:
        changed = False

        if (not result.account_id) and result.access_token:
            account_id = self._extract_account_id_from_access_token(result.access_token)
            if account_id:
                result.account_id = account_id
                changed = True
                self._log(f"{source_label} 从 access_token 回填 Account ID: {account_id}")

        if not result.workspace_id:
            try:
                workspace_id = str(self._get_workspace_id() or "").strip()
            except Exception as e:
                workspace_id = ""
                self._log(f"{source_label} 从授权 Cookie 回填 Workspace ID 失败: {e}", "warning")
            if workspace_id:
                result.workspace_id = workspace_id
                changed = True
                self._log(f"{source_label} 从当前 Cookie 回填 Workspace ID: {workspace_id}")

        need_backend_me = (not result.account_id) or (not result.workspace_id)
        if need_backend_me:
            me_payload = self._fetch_chatgpt_me_payload(result.access_token)
            if isinstance(me_payload, dict):
                if not result.account_id:
                    account_id = self._find_first_string_value(
                        me_payload,
                        ("chatgpt_account_id", "account_id"),
                    )
                    if account_id:
                        result.account_id = account_id
                        changed = True
                        self._log(f"{source_label} 从 backend-api/me 回填 Account ID: {account_id}")
                if not result.workspace_id:
                    workspace_id = self._extract_workspace_id_from_payload(me_payload)
                    if workspace_id:
                        result.workspace_id = workspace_id
                        changed = True
                        self._log(f"{source_label} 从 backend-api/me 回填 Workspace ID: {workspace_id}")

        if (not result.account_id) and self._create_account_account_id:
            result.account_id = str(self._create_account_account_id or "").strip()
            if result.account_id:
                changed = True
        if (not result.workspace_id) and self._create_account_workspace_id:
            result.workspace_id = str(self._create_account_workspace_id or "").strip()
            if result.workspace_id:
                changed = True
        return changed

    @staticmethod
    def _extract_personal_workspace_id_from_auth_session_payload(payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        account = payload.get("account")
        if not isinstance(account, dict):
            return ""
        structure = str(account.get("structure") or "").strip().lower()
        account_id = str(account.get("id") or "").strip()
        if structure == "personal" and account_id:
            return account_id
        return ""

    @staticmethod
    def _proxy_url_has_auth(proxy_url: Optional[str]) -> bool:
        candidate = str(proxy_url or "").strip()
        if not candidate:
            return False
        try:
            parsed = urllib.parse.urlparse(candidate if "://" in candidate else f"http://{candidate}")
        except Exception:
            return False
        return bool(parsed.username or parsed.password)

    @staticmethod
    def _sanitize_browser_profile_key(value: Optional[str]) -> str:
        candidate = str(value or "").strip()
        if not candidate:
            return "default"
        candidate = re.sub(r"[^A-Za-z0-9._-]+", "-", candidate).strip("._-")
        return candidate or "default"

    def _build_root_browser_user_data_dir(
        self,
        *,
        proxy_url: Optional[str] = None,
        purpose: str = "capture",
    ) -> Optional[str]:
        base_dir = str(self.registration_browser_persistent_profile_dir or "").strip()
        if not base_dir:
            return None

        profile_key = str(getattr(self.browser_profile, "proxy_ip", "") or "").strip()
        if not profile_key and proxy_url:
            try:
                parsed = urllib.parse.urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
                host = str(parsed.hostname or "").strip()
                port = str(parsed.port or "").strip()
                if host and port:
                    profile_key = f"{host}-{port}"
                else:
                    profile_key = host or port
            except Exception:
                profile_key = ""
        if not profile_key:
            profile_key = "direct"

        purpose_key = self._sanitize_browser_profile_key(purpose)
        profile_key = self._sanitize_browser_profile_key(profile_key)
        return os.path.join(base_dir, f"{purpose_key}-{profile_key}")

    def _create_root_browser_client(
        self,
        *,
        proxy_url: Optional[str] = None,
        purpose: str = "capture",
    ) -> Any:
        from http_client import BrowserClient

        return BrowserClient(
            proxy_url=proxy_url,
            browser_profile=self.browser_profile,
            headless=self.registration_browser_headless,
            user_data_dir=self._build_root_browser_user_data_dir(proxy_url=proxy_url, purpose=purpose),
        )

    def _infer_browser_otp_page_type(
        self,
        *,
        final_url: str,
        page_title: str = "",
        page_html: str = "",
    ) -> str:
        url_value = str(final_url or "").strip().lower()
        title_value = str(page_title or "").strip().lower()
        html_value = str(page_html or "").lower()

        if "auth.openai.com/add-phone" in url_value or "phone number required" in title_value:
            return "add_phone"
        if "auth.openai.com/about-you" in url_value or "finish creating account" in html_value or "confirm your age" in html_value:
            return "about_you"
        if "sign-in-with-chatgpt/codex/consent" in url_value:
            return "sign_in_with_chatgpt_codex_consent"
        if self._is_oauth_callback_url(final_url):
            return "oauth_callback"
        if "email-verification" in url_value or "email-otp" in url_value:
            return "email_otp_verification"
        if "auth.openai.com/log-in" in url_value:
            return "login"
        return ""

    def _try_validate_verification_code_with_browser(self, code: str) -> Optional[bool]:
        try:
            from http_client import HTTPClientError
        except Exception as e:
            self._log(f"浏览器 OTP 校验不可用: {e}", "warning")
            return None

        cookie_text = self._dump_session_cookies()
        session_hint = str(self.session_token or "").strip()
        device_hint = str(self.device_id or "").strip()
        if not device_hint:
            try:
                device_hint = str(self.session.cookies.get("oai-did") or "").strip()
            except Exception:
                device_hint = ""

        try:
            client = self._create_root_browser_client(proxy_url=self.proxy_url, purpose="otp")
        except Exception as e:
            self._log(f"浏览器 OTP 校验初始化失败: {e}", "warning")
            return None

        browser_result: Dict[str, Any] = {}
        try:
            raw_auth_url = str(
                getattr(self.oauth_start, "auth_url", "")
                or getattr(self.oauth_start, "url", "")
                or ""
            ).strip()
            preferred_auth_url = self._replace_oauth_authorize_prompt(raw_auth_url, None) or raw_auth_url
            browser_result = client.submit_openai_otp(
                cookies_text=cookie_text,
                otp_code=code,
                start_url=str(self._last_email_otp_start_url or "").strip() or None,
                auth_url=preferred_auth_url,
                session_token=session_hint,
                device_id=device_hint,
            )
        except HTTPClientError as e:
            self._log(f"浏览器 OTP 校验异常，回退 HTTP 主链: {e}", "warning")
            return None
        except Exception as e:
            self._log(f"浏览器 OTP 校验异常，回退 HTTP 主链: {e}", "warning")
            return None
        finally:
            try:
                client.close()
            except Exception:
                pass

        final_url = str(browser_result.get("final_url") or "").strip()
        auth_final_url = str(browser_result.get("auth_final_url") or "").strip()
        consent_final_url = str(browser_result.get("consent_final_url") or "").strip()
        observed_continue = self._pick_browser_observed_continue_url(browser_result)
        merged_cookie_text = str(browser_result.get("cookies_text") or "").strip()
        useful_browser_state = bool(
            browser_result.get("submitted")
            or browser_result.get("session_payload")
            or browser_result.get("backend_me_payload")
            or self._is_oauth_callback_url(observed_continue)
            or self._is_registration_gate_url(observed_continue)
            or self._is_oauth_callback_url(auth_final_url or final_url or consent_final_url)
            or self._is_registration_gate_url(auth_final_url or final_url or consent_final_url)
        )
        if merged_cookie_text and useful_browser_state:
            try:
                from http_client import _build_playwright_cookie_items

                self._merge_browser_cookies_into_session(_build_playwright_cookie_items(merged_cookie_text))
            except Exception:
                pass

        page_title = str(browser_result.get("page_title") or "").strip()
        page_html = str(browser_result.get("page_html") or "").strip()
        consent_html = str(browser_result.get("consent_html") or "").strip()
        derived_page_type = self._infer_browser_otp_page_type(
            final_url=auth_final_url or final_url,
            page_title=page_title,
            page_html=page_html,
        )
        self._last_validate_otp_page_type = derived_page_type or self._last_validate_otp_page_type
        if derived_page_type:
            self._log(f"浏览器 OTP 校验页面类型: {derived_page_type}")
            if self._is_add_phone_page_type(derived_page_type):
                self._log("浏览器 OTP 校验已明确落入 add_phone 门页", "warning")

        session_payload = browser_result.get("session_payload")
        backend_me_payload = browser_result.get("backend_me_payload")
        workspace_from_payload = ""
        if isinstance(session_payload, dict):
            workspace_from_payload = str(
                self._extract_workspace_id_from_payload(session_payload)
                or self._extract_personal_workspace_id_from_auth_session_payload(session_payload)
                or ""
            ).strip()
        if (not workspace_from_payload) and isinstance(backend_me_payload, dict):
            workspace_from_payload = str(self._extract_workspace_id_from_payload(backend_me_payload) or "").strip()
        if (not workspace_from_payload) and consent_html:
            workspace_from_payload = str(self._extract_workspace_id_from_text(consent_html) or "").strip()
        if workspace_from_payload:
            self._last_validate_otp_workspace_id = workspace_from_payload
            self._log(f"浏览器 OTP 校验返回 Workspace ID: {workspace_from_payload}")

        preferred_continue = self._pick_preferred_continue_url(
            observed_continue,
            auth_final_url,
            final_url,
            consent_final_url,
        )
        if preferred_continue and (
            self._is_oauth_callback_url(preferred_continue)
            or self._is_registration_gate_url(preferred_continue)
            or ("sign-in-with-chatgpt/codex/consent" in preferred_continue)
            or ("auth.openai.com/about-you" in preferred_continue)
        ):
            self._last_validate_otp_continue_url = preferred_continue
            self._log(f"浏览器 OTP 校验返回 continue_url: {preferred_continue[:100]}...")

        self._refresh_auth_cookie_workspace_diagnostics(source_label="浏览器 OTP 校验后")

        if not bool(browser_result.get("submitted")):
            final_marker = final_url or auth_final_url or consent_final_url
            self._log(
                f"浏览器 OTP 校验未找到验证码输入框，回退 HTTP 主链 (final={final_marker[:120]}...)",
                "warning",
            )
            return None

        success = bool(
            self._is_oauth_callback_url(preferred_continue)
            or self._is_registration_gate_url(preferred_continue)
            or ("sign-in-with-chatgpt/codex/consent" in preferred_continue)
            or ("auth.openai.com/about-you" in preferred_continue)
            or bool(session_payload)
            or bool(backend_me_payload)
            or bool(consent_html)
        )
        if success:
            final_marker = preferred_continue or final_url or auth_final_url or consent_final_url
            self._log(f"浏览器 OTP 校验完成，final={final_marker[:120]}...")
        else:
            final_marker = preferred_continue or final_url or auth_final_url or consent_final_url
            self._log(f"浏览器 OTP 校验未完成预期跳转，final={final_marker[:120]}...", "warning")
        return success

    def _try_capture_with_root_browser_client(
        self,
        result: RegistrationResult,
        *,
        stage_label: str,
        auth_url: Optional[str] = None,
        continue_url: Optional[str] = None,
    ) -> bool:
        try:
            from http_client import HTTPClientError
        except Exception as e:
            self._log(f"{stage_label} BrowserClient 不可用: {e}", "warning")
            return False

        cookie_text = self._dump_session_cookies()
        session_hint = str(result.session_token or self.session_token or "").strip()
        device_hint = str(result.device_id or self.device_id or "").strip()
        if not device_hint:
            try:
                device_hint = str(self.session.cookies.get("oai-did") or "").strip()
            except Exception:
                device_hint = ""

        browser_state = None
        attempts: List[Tuple[Optional[str], str]] = [(self.proxy_url, "代理")]
        if self.proxy_url:
            attempts.append((None, "直连"))

        for attempt_proxy, attempt_label in attempts:
            try:
                purpose = "capture" if attempt_proxy else "capture-direct"
                client = self._create_root_browser_client(proxy_url=attempt_proxy, purpose=purpose)
                try:
                    browser_state = client.capture_openai_state(
                        cookies_text=cookie_text,
                        continue_url=continue_url,
                        auth_url=auth_url,
                        session_token=session_hint,
                        device_id=device_hint,
                    )
                finally:
                    client.close()
            except Exception as e:
                self._log(f"{stage_label} BrowserClient {attempt_label}捕获失败: {e}", "warning")
                browser_state = None
                continue

            if isinstance(browser_state, dict) and browser_state.get("success"):
                if attempt_proxy is None and self.proxy_url:
                    self._log(f"{stage_label} BrowserClient 直连重试已拿到页面状态", "warning")
                break

            browser_state = None

        if not isinstance(browser_state, dict) or not browser_state.get("success"):
            self._log(f"{stage_label} BrowserClient 未返回有效会话状态", "warning")
            return False

        session_payload = browser_state.get("session_payload")
        if isinstance(session_payload, dict):
            session_tokens = self._extract_tokens_from_auth_session_payload(session_payload)
            access_token = str(session_tokens.get("access_token") or "").strip()
            if access_token:
                result.access_token = access_token

            session_token = str(session_payload.get("sessionToken") or session_hint or "").strip()
            if session_token:
                result.session_token = session_token
                self.session_token = session_token

            account_id = self._find_first_string_value(session_payload, ("chatgpt_account_id", "account_id"))
            if not account_id:
                account_node = session_payload.get("account")
                if isinstance(account_node, dict):
                    account_id = str(account_node.get("id") or "").strip()
            if account_id:
                result.account_id = str(result.account_id or account_id).strip()
                self._create_account_account_id = self._create_account_account_id or account_id
                self._log(f"{stage_label} BrowserClient Session Account ID: {account_id}")

            workspace_id = str(
                self._extract_workspace_id_from_payload(session_payload)
                or self._extract_personal_workspace_id_from_auth_session_payload(session_payload)
                or ""
            ).strip()
            if workspace_id:
                result.workspace_id = workspace_id
                self._create_account_workspace_id = self._create_account_workspace_id or workspace_id
                self._log(f"{stage_label} BrowserClient Session Workspace ID: {workspace_id}")

        consent_html = str(browser_state.get("consent_html") or "").strip()
        if (not result.workspace_id) and consent_html:
            workspace_from_html = str(self._extract_workspace_id_from_text(consent_html) or "").strip()
            if workspace_from_html:
                result.workspace_id = workspace_from_html
                self._create_account_workspace_id = self._create_account_workspace_id or workspace_from_html
                self._log(f"{stage_label} BrowserClient consent 提取 Workspace ID: {workspace_from_html}")

        auth_final_url = str(browser_state.get("auth_final_url") or "").strip()
        observed_continue = self._pick_browser_observed_continue_url(browser_state)
        if self._is_local_oauth_callback_url(observed_continue):
            token_info = self._handle_oauth_callback(observed_continue)
            if token_info:
                self._merge_token_info_into_result(result, token_info)
                self._log(f"{stage_label} BrowserClient 命中观测到的本地 OAuth callback")
                return True

        if self._is_oauth_callback_url(observed_continue):
            self._consume_oauth_callback_for_session(
                result,
                observed_continue,
                stage_label=f"{stage_label} BrowserClient",
                referer=continue_url or auth_url or "https://chatgpt.com/",
            )

        if self._is_local_oauth_callback_url(auth_final_url):
            token_info = self._handle_oauth_callback(auth_final_url)
            if token_info:
                self._merge_token_info_into_result(result, token_info)
                self._log(f"{stage_label} BrowserClient 命中本地 OAuth callback")
                return True

        if self._is_oauth_callback_url(auth_final_url):
            self._consume_oauth_callback_for_session(
                result,
                auth_final_url,
                stage_label=f"{stage_label} BrowserClient",
                referer=continue_url or auth_url or "https://chatgpt.com/",
            )

        return bool(result.access_token or result.workspace_id or result.refresh_token)

    @staticmethod
    def _replace_oauth_authorize_prompt(auth_url: str, prompt: Optional[str]) -> str:
        candidate = str(auth_url or "").strip()
        if not candidate:
            return ""

        parsed = urllib.parse.urlparse(candidate)
        query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        filtered_pairs = [(key, value) for key, value in query_pairs if str(key or "").strip().lower() != "prompt"]
        if prompt is not None:
            filtered_pairs.append(("prompt", str(prompt)))
        new_query = urllib.parse.urlencode(filtered_pairs)
        return urllib.parse.urlunparse(parsed._replace(query=new_query))

    def _build_authenticated_oauth_authorize_candidates(self, auth_url: str) -> List[str]:
        candidates: List[str] = []
        seen: set[str] = set()

        def _push(url: str) -> None:
            candidate = str(url or "").strip()
            if not candidate or candidate in seen:
                return
            seen.add(candidate)
            candidates.append(candidate)

        _push(self._replace_oauth_authorize_prompt(auth_url, "none"))
        _push(self._replace_oauth_authorize_prompt(auth_url, None))
        _push(auth_url)
        return candidates

    def _backfill_oauth_tokens_from_authenticated_session(
        self,
        result: RegistrationResult,
        *,
        source_label: str,
    ) -> bool:
        if result.refresh_token:
            return True

        oauth_start = self.oauth_start
        auth_url = str(
            getattr(oauth_start, "auth_url", "")
            or getattr(oauth_start, "url", "")
            or ""
        ).strip()
        if auth_url:
            self._log(f"{source_label} OAuth 回填优先复用当前授权上下文", "warning")
        else:
            try:
                oauth_start = self.oauth_manager.start_oauth()
                self.oauth_start = oauth_start
            except Exception as e:
                self._log(f"{source_label} 无法启动 OAuth 回填流程: {e}", "warning")
                return False

            auth_url = str(
                getattr(oauth_start, "auth_url", "")
                or getattr(oauth_start, "url", "")
                or ""
            ).strip()
        if not auth_url:
            self._log(f"{source_label} OAuth 回填未拿到 authorize URL", "warning")
            return False

        workspace_id = ""
        try:
            workspace_id = str(self._get_workspace_id() or "").strip()
        except Exception as e:
            self._log(f"{source_label} OAuth 回填预取 Workspace ID 失败: {e}", "warning")

        workspace_continue = ""
        workspace_candidates: List[str] = []
        seen_workspace_candidates: set[str] = set()

        def _push_workspace_candidate(raw_value: Optional[str]) -> None:
            candidate = str(raw_value or "").strip()
            if not candidate or candidate in seen_workspace_candidates:
                return
            seen_workspace_candidates.add(candidate)
            workspace_candidates.append(candidate)

        _push_workspace_candidate(workspace_id)
        if not workspace_id:
            _push_workspace_candidate(result.workspace_id)
            _push_workspace_candidate(self._create_account_workspace_id)
            _push_workspace_candidate(result.account_id)
            _push_workspace_candidate(self._create_account_account_id)

        for candidate_workspace in workspace_candidates:
            continue_candidate = str(self._select_workspace(candidate_workspace) or "").strip()
            if not continue_candidate:
                continue
            workspace_continue = continue_candidate
            auth_url = self._pick_preferred_continue_url(continue_candidate, auth_url)
            if candidate_workspace != workspace_id:
                self._log(
                    f"{source_label} OAuth 回填使用候选 workspace/account ID 命中 continue_url: {candidate_workspace}",
                    "warning",
                )
            else:
                self._log(f"{source_label} OAuth 回填优先复用 workspace/select 返回的 continue_url", "warning")
            if candidate_workspace:
                result.workspace_id = candidate_workspace
                self._create_account_workspace_id = self._create_account_workspace_id or candidate_workspace
            break

        auth_candidates = self._build_authenticated_oauth_authorize_candidates(auth_url)
        self._log(
            f"{source_label} 已建立 ChatGPT session，尝试补跑已登录 OAuth 回填 refresh_token（候选 {len(auth_candidates)} 条）...",
            "warning",
        )

        browser_continue = self._pick_preferred_continue_url(
            workspace_continue,
            self._create_account_continue_url,
            self._last_validate_otp_continue_url,
        )
        browser_auth_url = auth_candidates[0] if auth_candidates else auth_url
        if self.registration_browser_first_enabled:
            browser_captured = self._try_capture_with_root_browser_client(
                result,
                stage_label=source_label,
                auth_url=browser_auth_url,
                continue_url=browser_continue,
            )
            if browser_captured and result.refresh_token:
                return True

        for attempt_idx, auth_candidate in enumerate(auth_candidates, start=1):
            self._log(f"{source_label} OAuth 回填尝试 {attempt_idx}/{len(auth_candidates)}: {auth_candidate[:140]}...", "warning")
            callback_url, final_url = self._follow_redirects(auth_candidate)
            oauth_callback = str(callback_url or final_url or "").strip()
            if not oauth_callback:
                self._log(f"{source_label} OAuth 回填未命中 callback，final={str(final_url or '')[:120]}...", "warning")
                continue

            callback_has_error = ("error=" in oauth_callback) and ("code=" not in oauth_callback)
            if callback_has_error:
                self._log(f"{source_label} OAuth 回填 callback 返回错误参数: {oauth_callback[:140]}...", "warning")
                continue

            if not self._is_local_oauth_callback_url(oauth_callback):
                if self._is_oauth_callback_url(oauth_callback):
                    self._log(f"{source_label} OAuth 回填命中 ChatGPT callback，仅补 session，不做本地 token exchange", "warning")
                    self._consume_oauth_callback_for_session(
                        result,
                        oauth_callback,
                        stage_label=f"{source_label} OAuth回填",
                        referer=final_url or auth_candidate,
                    )
                    if result.refresh_token:
                        return True
                    continue

                self._log(f"{source_label} OAuth 回填未命中本地 callback: {oauth_callback[:140]}...", "warning")
                continue

            token_info = self._handle_oauth_callback(oauth_callback)
            if not token_info:
                self._log(f"{source_label} OAuth 回填 token exchange 失败", "warning")
                continue

            self._merge_token_info_into_result(result, token_info)
            if result.refresh_token:
                return True

        if self.registration_browser_first_enabled:
            if workspace_continue:
                self._log(f"{source_label} OAuth 回填仍未拿到 refresh_token，最后一次 continue_url: {workspace_continue[:140]}...", "warning")
            return bool(result.refresh_token)

        browser_captured = self._try_capture_with_root_browser_client(
            result,
            stage_label=source_label,
            auth_url=browser_auth_url,
            continue_url=browser_continue,
        )
        if browser_captured and result.refresh_token:
            return True

        if workspace_continue:
            self._log(f"{source_label} OAuth 回填仍未拿到 refresh_token，最后一次 continue_url: {workspace_continue[:140]}...", "warning")
        return bool(result.refresh_token)

    def _process_oauth_callback_result(
        self,
        result: RegistrationResult,
        callback_url: str,
        *,
        stage_label: str,
        referer: Optional[str] = None,
    ) -> bool:
        callback = str(callback_url or "").strip()
        if not self._is_oauth_callback_url(callback):
            return False

        processed = self._consume_oauth_callback_for_session(
            result,
            callback,
            stage_label=stage_label,
            referer=referer,
        )
        if self._is_local_oauth_callback_url(callback):
            token_info = self._handle_oauth_callback(callback)
            if token_info:
                self._merge_token_info_into_result(result, token_info)
                return True
            return processed

        self._log(f"{stage_label} 命中 ChatGPT callback，改用当前会话补跑已登录 OAuth 回填", "warning")
        self._warmup_chatgpt_session()
        self._capture_auth_session_tokens(
            result,
            access_hint=result.access_token,
            referer=referer or callback,
        )
        self._backfill_oauth_tokens_from_authenticated_session(result, source_label=stage_label)
        self._backfill_identity_from_current_session(result, source_label=stage_label)
        return bool(processed or result.access_token or result.refresh_token)

    def _try_complete_from_otp_callback(self, result: RegistrationResult, stage_label: str) -> bool:
        preferred_callback = self._pick_preferred_continue_url(
            self._last_validate_otp_continue_url,
            self._create_account_continue_url,
        )
        if not self._is_oauth_callback_url(preferred_callback):
            return False

        callback_has_error = ("error=" in preferred_callback) and ("code=" not in preferred_callback)
        if callback_has_error:
            self._log(f"{stage_label}返回 callback 但携带错误参数，跳过直连补 token: {preferred_callback[:140]}...", "warning")
        else:
            if self.registration_browser_first_enabled and (not self._is_local_oauth_callback_url(preferred_callback)):
                self._log(f"{stage_label}返回 OAuth callback，先用 BrowserClient 复用当前 callback/session...", "warning")
                browser_captured = self._try_capture_with_root_browser_client(
                    result,
                    stage_label=stage_label,
                    auth_url=preferred_callback,
                )
                if browser_captured:
                    self._backfill_identity_from_current_session(result, source_label=stage_label)
                    if result.access_token and not result.refresh_token:
                        self._backfill_oauth_tokens_from_authenticated_session(result, source_label=stage_label)
                        self._backfill_identity_from_current_session(result, source_label=stage_label)
                    if result.refresh_token:
                        self._log(f"{stage_label} BrowserClient 已补齐 session/access/refresh")
                        return True
                    if result.access_token and result.session_token:
                        self._log(f"{stage_label} BrowserClient 已补到 session/access，但 refresh_token 仍缺失，继续走 workspace 兜底", "warning")
                        return False
            self._log(f"{stage_label}返回 OAuth callback，优先复用当前 callback/session...", "warning")
            if not self._process_oauth_callback_result(
                result,
                preferred_callback,
                stage_label=stage_label,
                referer="https://chatgpt.com/auth/login",
            ):
                self._log(f"{stage_label} callback 处理失败，继续走 workspace 兜底链路", "warning")

        self._warmup_chatgpt_session()
        if self._capture_auth_session_tokens(
            result,
            access_hint=result.access_token,
            referer=preferred_callback,
        ):
            self._backfill_identity_from_current_session(result, source_label=stage_label)
            if not result.refresh_token:
                self._backfill_oauth_tokens_from_authenticated_session(result, source_label=stage_label)
                self._backfill_identity_from_current_session(result, source_label=stage_label)
            if result.refresh_token:
                self._log(f"{stage_label} callback 已补齐 session/access/refresh")
                return True
            self._log(f"{stage_label} callback 已补到 session/access，但 refresh_token 仍缺失，继续走 workspace 兜底", "warning")
            return False

        if result.access_token:
            self._log(f"{stage_label} callback 已补到 access_token，但 session_token 仍缺失，继续走 workspace 兜底", "warning")
        return False

    def _try_complete_session_from_registration_gate(self, result: RegistrationResult, gate_url: str) -> bool:
        gate = str(gate_url or "").strip()
        if not self._is_registration_gate_url(gate):
            return False

        self._log("会话桥接落在 about-you/add-phone 门页，先顺着门页补齐 session/token...", "warning")
        if self.registration_browser_first_enabled:
            browser_captured = self._try_capture_with_root_browser_client(
                result,
                stage_label="门页桥接",
                continue_url=gate,
            )
            if browser_captured:
                self._backfill_identity_from_current_session(result, source_label="门页桥接")
                if result.access_token and not result.refresh_token:
                    self._backfill_oauth_tokens_from_authenticated_session(result, source_label="门页桥接")
                    self._backfill_identity_from_current_session(result, source_label="门页桥接")
                if result.session_token and result.access_token:
                    self._log("门页桥接已通过 BrowserClient 补齐 session/access", "warning")
                    return True
                if result.access_token:
                    self._log("门页桥接 BrowserClient 已拿到 access_token，但 session_token 仍缺失", "warning")

        try:
            self.session.get(
                gate,
                headers={
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "referer": gate,
                    "user-agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                    ),
                },
                allow_redirects=True,
                timeout=20,
            )
        except Exception as e:
            self._log(f"门页桥接补跳异常: {e}", "warning")

        if self._capture_auth_session_tokens(result, access_hint=result.access_token, referer=gate):
            self._log("门页桥接已补齐 session/access")
            return True

        self._warmup_chatgpt_session()
        if self._capture_auth_session_tokens(result, access_hint=result.access_token, referer=gate):
            self._log("门页桥接经首页预热后补齐 session/access")
            return True

        if self.registration_browser_first_enabled:
            return False

        browser_captured = self._try_capture_with_root_browser_client(
            result,
            stage_label="门页桥接",
            continue_url=gate,
        )
        if browser_captured:
            self._backfill_identity_from_current_session(result, source_label="门页桥接")
            if result.access_token and not result.refresh_token:
                self._backfill_oauth_tokens_from_authenticated_session(result, source_label="门页桥接")
                self._backfill_identity_from_current_session(result, source_label="门页桥接")
            if result.session_token and result.access_token:
                self._log("门页桥接已通过 BrowserClient 补齐 session/access", "warning")
                return True
            if result.access_token:
                self._log("门页桥接 BrowserClient 已拿到 access_token，但 session_token 仍缺失", "warning")
        return False

    def _finalize_result_with_current_tokens(
        self,
        result: RegistrationResult,
        *,
        workspace_hint: Optional[str] = None,
        source: Optional[str] = None,
    ) -> None:
        source_label = str(source or "当前会话").strip() or "当前会话"
        self._backfill_identity_from_current_session(result, source_label=source_label)
        if (not result.refresh_token) and result.access_token:
            self._backfill_oauth_tokens_from_authenticated_session(result, source_label=source_label)
            self._backfill_identity_from_current_session(result, source_label=source_label)
        if not result.account_id:
            result.account_id = str(self._create_account_account_id or "").strip()
        if not result.workspace_id:
            result.workspace_id = str(
                workspace_hint
                or self._last_validate_otp_workspace_id
                or self._create_account_workspace_id
                or ""
            ).strip()
        if not result.workspace_id:
            try:
                result.workspace_id = str(self._get_workspace_id() or "").strip()
            except Exception as e:
                self._log(f"补齐当前 token 结果时获取 workspace_id 失败: {e}", "warning")
        if not result.refresh_token:
            result.refresh_token = str(self._create_account_refresh_token or "").strip()
        result.password = self.password or ""
        result.source = source or ("login" if self._is_existing_account else "register")
        result.device_id = result.device_id or str(self.device_id or self.session.cookies.get("oai-did") or "")

        session_cookie = self.session.cookies.get("__Secure-next-auth.session-token")
        if session_cookie:
            self.session_token = session_cookie
            result.session_token = session_cookie
            self._log("Session Token 也捞到了，今天这网没白连")

    def _capture_auth_session_tokens(
        self,
        result: RegistrationResult,
        access_hint: Optional[str] = None,
        referer: Optional[str] = None,
    ) -> bool:
        """
        直接通过 /api/auth/session 捕获 session_token + access_token。
        这是 ABCard Phase 1 的关键路径。
        """
        access_token = str(access_hint or "").strip()
        referer_url = str(referer or "https://chatgpt.com/").strip() or "https://chatgpt.com/"
        set_cookie_text = ""
        request_cookie_text = ""
        try:
            headers = {
                "accept": "application/json",
                "referer": referer_url,
                "origin": "https://chatgpt.com",
                "user-agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                "cache-control": "no-cache",
                "pragma": "no-cache",
            }
            if access_token:
                headers["authorization"] = f"Bearer {access_token}"
            response = self.session.get(
                "https://chatgpt.com/api/auth/session",
                headers=headers,
                timeout=20,
            )
            set_cookie_text = self._flatten_set_cookie_headers(response)
            request_cookie_text = self._extract_request_cookie_header(response)
            if response.status_code == 200:
                try:
                    data = response.json() or {}
                    session_tokens = self._extract_tokens_from_auth_session_payload(data)
                    access_from_json = str(session_tokens.get("access_token") or "").strip()
                    if access_from_json:
                        access_token = access_from_json
                    account_from_json = self._find_first_string_value(
                        data,
                        ("chatgpt_account_id", "account_id"),
                    )
                    if not account_from_json:
                        account_node = data.get("account")
                        if isinstance(account_node, dict):
                            account_from_json = str(account_node.get("id") or "").strip()
                    if (not account_from_json) and access_token:
                        account_from_json = str(self._extract_account_id_from_access_token(access_token) or "").strip()
                    if account_from_json:
                        if not result.account_id:
                            result.account_id = account_from_json
                        if not self._create_account_account_id:
                            self._create_account_account_id = account_from_json
                        self._log(f"Session Account ID: {account_from_json}")
                    workspace_from_json = str(
                        self._extract_workspace_id_from_payload(data)
                        or self._extract_personal_workspace_id_from_auth_session_payload(data)
                        or ""
                    ).strip()
                    if workspace_from_json:
                        if not result.workspace_id:
                            result.workspace_id = workspace_from_json
                        if not self._create_account_workspace_id:
                            self._create_account_workspace_id = workspace_from_json
                        self._log(f"Session Workspace ID: {workspace_from_json}")
                    refresh_from_json = str(session_tokens.get("refresh_token") or "").strip()
                    if refresh_from_json and not result.refresh_token:
                        result.refresh_token = refresh_from_json
                    id_from_json = str(session_tokens.get("id_token") or "").strip()
                    if id_from_json and not result.id_token:
                        result.id_token = id_from_json
                except Exception:
                    pass
            else:
                self._log(f"/api/auth/session 返回异常状态: {response.status_code}", "warning")
        except Exception as e:
            self._log(f"获取 auth/session 失败: {e}", "warning")

        # 1) 直接从 cookie jar 拿
        session_token = self._extract_session_token_from_cookie_jar(self.session.cookies)

        # 2) 从完整 cookies 文本兜底（含分片）
        if not session_token:
            session_token = self._extract_session_token_from_cookie_text(self._dump_session_cookies())

        # 3) 从 set-cookie 兜底（含分片）
        if not session_token and set_cookie_text:
            session_token = self._extract_session_token_from_cookie_text(set_cookie_text)

        # 4) 从请求 Cookie 头兜底（对齐 F12 Network 观测）
        if not session_token and request_cookie_text:
            session_token = self._extract_session_token_from_cookie_text(request_cookie_text)

        # 兜底：已有 access_token 但无 session_token 时，带 Bearer 再请求一次 auth/session
        if (not session_token) and access_token:
            try:
                retry_response = self.session.get(
                    "https://chatgpt.com/api/auth/session",
                    headers={
                        "accept": "application/json",
                        "referer": referer_url,
                        "origin": "https://chatgpt.com",
                        "user-agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                        ),
                        "authorization": f"Bearer {access_token}",
                        "cache-control": "no-cache",
                        "pragma": "no-cache",
                    },
                    timeout=20,
                )
                retry_set_cookie = self._flatten_set_cookie_headers(retry_response)
                retry_request_cookie = self._extract_request_cookie_header(retry_response)
                if retry_response.status_code == 200:
                    try:
                        retry_data = retry_response.json() or {}
                        retry_tokens = self._extract_tokens_from_auth_session_payload(retry_data)
                        retry_access = str(retry_tokens.get("access_token") or "").strip()
                        if retry_access:
                            access_token = retry_access
                        retry_account = self._find_first_string_value(
                            retry_data,
                            ("chatgpt_account_id", "account_id"),
                        )
                        if not retry_account:
                            retry_account_node = retry_data.get("account")
                            if isinstance(retry_account_node, dict):
                                retry_account = str(retry_account_node.get("id") or "").strip()
                        if (not retry_account) and access_token:
                            retry_account = str(self._extract_account_id_from_access_token(access_token) or "").strip()
                        if retry_account:
                            if not result.account_id:
                                result.account_id = retry_account
                            if not self._create_account_account_id:
                                self._create_account_account_id = retry_account
                            self._log(f"Session Account ID: {retry_account}")
                        retry_workspace = str(
                            self._extract_workspace_id_from_payload(retry_data)
                            or self._extract_personal_workspace_id_from_auth_session_payload(retry_data)
                            or ""
                        ).strip()
                        if retry_workspace:
                            if not result.workspace_id:
                                result.workspace_id = retry_workspace
                            if not self._create_account_workspace_id:
                                self._create_account_workspace_id = retry_workspace
                            self._log(f"Session Workspace ID: {retry_workspace}")
                        retry_refresh = str(retry_tokens.get("refresh_token") or "").strip()
                        if retry_refresh and not result.refresh_token:
                            result.refresh_token = retry_refresh
                        retry_id = str(retry_tokens.get("id_token") or "").strip()
                        if retry_id and not result.id_token:
                            result.id_token = retry_id
                    except Exception:
                        pass
                if not session_token:
                    session_token = self._extract_session_token_from_cookie_jar(self.session.cookies)
                if not session_token:
                    session_token = self._extract_session_token_from_cookie_text(self._dump_session_cookies())
                if not session_token and retry_set_cookie:
                    session_token = self._extract_session_token_from_cookie_text(retry_set_cookie)
                if not session_token and retry_request_cookie:
                    session_token = self._extract_session_token_from_cookie_text(retry_request_cookie)
            except Exception as e:
                self._log(f"Bearer 兜底换 session_token 失败: {e}", "warning")

        if not session_token:
            cookies_text = self._dump_session_cookies()
            raw_direct_match = re.search(
                r"(?:^|[;,]\s*)(?:__|_)Secure-next-auth\.session-token=([^;,]*)",
                cookies_text,
            )
            raw_direct_len = len(str(raw_direct_match.group(1) or "").strip()) if raw_direct_match else 0
            chunk_count = len(re.findall(r"(?:__|_)Secure-next-auth\.session-token\.(\d+)=", cookies_text))
            req_cookie_len = len(str(request_cookie_text or "").strip())
            self._log(
                f"auth/session 仍未命中 session_token（raw_direct_len={raw_direct_len}, chunks={chunk_count}, req_cookie_len={req_cookie_len}）",
                "warning",
            )

        # 设备 ID 同步
        did = ""
        try:
            did = str(self.session.cookies.get("oai-did") or "").strip()
        except Exception:
            did = ""
        if did:
            self.device_id = did
            result.device_id = did

        if session_token:
            self.session_token = session_token
            result.session_token = session_token
        if access_token:
            result.access_token = access_token

        self._log(
            "Auth Session 捕获结果: session_token="
            + ("有" if bool(result.session_token) else "无")
            + ", access_token="
            + ("有" if bool(result.access_token) else "无")
        )
        return bool(result.session_token and result.access_token)

    def _bootstrap_chatgpt_signin_for_session(self, result: RegistrationResult) -> bool:
        """
        对齐 ABCard 的补会话路径：
        csrf -> signin/openai -> 跟随跳转 -> auth/session，目标是拿到 session_token。
        """
        self._log("Session Token 还没就位，尝试 ABCard 同款会话桥接...")
        self._warmup_chatgpt_session()
        csrf_token = ""
        auth_url = ""
        try:
            csrf_resp = self.session.get(
                "https://chatgpt.com/api/auth/csrf",
                headers={
                    "accept": "application/json",
                    "referer": "https://chatgpt.com/auth/login",
                    "origin": "https://chatgpt.com",
                    "user-agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                    ),
                },
                timeout=20,
            )
            if csrf_resp.status_code == 200:
                csrf_token = str((csrf_resp.json() or {}).get("csrfToken") or "").strip()
            else:
                self._log(f"csrf 获取失败: HTTP {csrf_resp.status_code}", "warning")
        except Exception as e:
            self._log(f"csrf 获取异常: {e}", "warning")

        if not csrf_token:
            self._log("csrf token 为空，跳过会话桥接", "warning")
            return False

        try:
            signin_resp = self.session.post(
                "https://chatgpt.com/api/auth/signin/openai",
                headers={
                    "accept": "application/json",
                    "content-type": "application/x-www-form-urlencoded",
                    "origin": "https://chatgpt.com",
                    "referer": "https://chatgpt.com/auth/login",
                    "user-agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                    ),
                },
                data={
                    "csrfToken": csrf_token,
                    "callbackUrl": "https://chatgpt.com/",
                    "json": "true",
                },
                timeout=20,
            )
            if signin_resp.status_code == 200:
                auth_url = str((signin_resp.json() or {}).get("url") or "").strip()
            else:
                self._log(f"signin/openai 失败: HTTP {signin_resp.status_code}", "warning")
        except Exception as e:
            self._log(f"signin/openai 异常: {e}", "warning")

        if not auth_url:
            self._log("signin/openai 未返回 auth_url，跳过会话桥接", "warning")
            return False

        callback_url = ""
        final_url = auth_url
        try:
            callback_url, final_url = self._follow_chatgpt_auth_redirects(auth_url)
        except Exception as e:
            self._log(f"会话桥接重定向跟踪异常: {e}", "warning")
            callback_url = ""
            final_url = auth_url

        # 若已拿到 callback，补打一跳确保 next-auth callback 被完整执行。
        if callback_url and "error=" not in callback_url:
            try:
                self.session.get(
                    callback_url,
                    headers={
                        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "referer": "https://chatgpt.com/auth/login",
                        "user-agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                        ),
                    },
                    allow_redirects=True,
                    timeout=25,
                )
            except Exception as e:
                self._log(f"会话桥接 callback 补跳异常: {e}", "warning")
        elif callback_url and "error=" in callback_url:
            self._log(f"会话桥接回调返回错误参数: {callback_url[:140]}...", "warning")
        else:
            self._log(f"会话桥接未命中 callback，final_url={final_url[:120]}...", "warning")
            # 命中 auth.openai 登录页时，尝试自动登录补会话（对齐 ABCard 的登录态建立思路）。
            if "auth.openai.com/log-in" in str(final_url or "").lower():
                self._log("会话桥接进入登录页，尝试自动登录后继续抓取 session_token...")
                if self._bridge_login_for_session_token(result):
                    return True
            elif self._is_registration_gate_url(final_url):
                if self._try_complete_session_from_registration_gate(result, final_url):
                    return True
                self._log("门页桥接未补齐 session/token，回退自动登录桥接再试一次...", "warning")
                if self._bridge_login_for_session_token(result):
                    return True

        self._warmup_chatgpt_session()
        cookie_text = self._dump_session_cookies()
        direct_token = self._extract_session_token_from_cookie_text(cookie_text)
        has_direct = bool(direct_token)
        chunk_count = len(re.findall(r"(?:__|_)Secure-next-auth\.session-token\.(\d+)=", cookie_text))
        if direct_token and not result.session_token:
            self.session_token = direct_token
            result.session_token = direct_token
            self._log(f"会话桥接已缓存 session_token（len={len(direct_token)}）")
        self._log(
            f"会话桥接后 cookie 概览: direct={'有' if has_direct else '无'}, chunks={chunk_count}"
        )
        return self._capture_auth_session_tokens(result, access_hint=result.access_token)

    def _bridge_login_for_session_token(self, result: RegistrationResult) -> bool:
        """
        当 chatgpt signin/openai 跳回 auth.openai 登录页时，自动补一次登录流程：
        login -> password -> email otp -> workspace -> auth/session。
        """
        try:
            if not self.email or not self.password:
                self._log("会话桥接自动登录缺少邮箱或密码，无法继续", "warning")
                return False

            did = ""
            try:
                did = str(self.session.cookies.get("oai-did") or "").strip()
            except Exception:
                did = ""
            if not did:
                did = str(uuid.uuid4())
                try:
                    self.session.cookies.set("oai-did", did, domain=".chatgpt.com", path="/")
                except Exception:
                    pass
            self.device_id = did
            result.device_id = result.device_id or did

            sen_token = self._check_sentinel(did)
            login_start_result = self._submit_login_start(did, sen_token)
            if not login_start_result.success:
                self._log(
                    f"会话桥接自动登录入口失败: {login_start_result.error_message}",
                    "warning",
                )
                return False
            page_type = str(login_start_result.page_type or "").strip()
            if self._is_email_otp_page_type(page_type):
                self._log("会话桥接自动登录已直达邮箱验证码页，跳过密码提交")
            elif self._is_login_password_page_type(page_type):
                password_result = self._submit_login_password()
                if not password_result.success:
                    self._log(
                        f"会话桥接自动登录提交密码失败: {password_result.error_message}",
                        "warning",
                    )
                    return False
                if not password_result.is_existing_account:
                    self._log(
                        f"会话桥接自动登录未进入邮箱验证码页: {password_result.page_type or 'unknown'}",
                        "warning",
                    )
                    return False
            else:
                self._log(
                    f"会话桥接自动登录入口返回未知页面: {page_type or 'unknown'}",
                    "warning",
                )
                return False

            if not self._verify_email_otp_with_retry(stage_label="会话桥接登录验证码", max_attempts=3):
                self._log("会话桥接自动登录验证码校验失败", "warning")
                return False

            if self._try_complete_from_otp_callback(result, stage_label="登录 OTP"):
                return True

            # OTP 成功后先直接抓一次 auth/session，避免无谓依赖 workspace 流程。
            self._warmup_chatgpt_session()
            if self._capture_auth_session_tokens(result, access_hint=result.access_token):
                self._log("会话桥接自动登录在 OTP 后已命中 session_token")
                return True

            workspace_id = self._get_workspace_id()
            if not workspace_id:
                workspace_id = str(result.workspace_id or "").strip()
                if workspace_id:
                    self._log(f"会话桥接自动登录复用已知 workspace_id: {workspace_id}")
            if not workspace_id:
                self._log("会话桥接自动登录未获取到 workspace_id", "warning")
                return False
            result.workspace_id = workspace_id

            continue_url = self._select_workspace(workspace_id)
            if not continue_url:
                cached_continue = str(self._create_account_continue_url or "").strip()
                if cached_continue:
                    continue_url = cached_continue
                    self._log("会话桥接自动登录未获取到 continue_url，改用 create_account 缓存 continue_url", "warning")
                else:
                    self._log("会话桥接自动登录未获取到 continue_url", "warning")
                    return False

            callback_url, final_url = self._follow_redirects(continue_url)
            self._log(
                f"会话桥接自动登录重定向完成: callback={'有' if callback_url else '无'}, final={str(final_url or '')[:100]}..."
            )

            self._warmup_chatgpt_session()
            return self._capture_auth_session_tokens(result, access_hint=result.access_token)
        except Exception as e:
            self._log(f"会话桥接自动登录异常: {e}", "warning")
            return False

    def _follow_chatgpt_auth_redirects(self, start_url: str) -> Tuple[str, str]:
        """
        对齐 ABCard 的 next-auth 重定向跟踪：
        - 手动跟踪 30x
        - 识别 /api/auth/callback/openai
        Returns:
            (callback_url, final_url)
        """
        import urllib.parse

        current_url = str(start_url or "").strip()
        callback_url = ""
        bridged_header_token = ""
        if not current_url:
            return "", ""

        max_redirects = 12
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )
        for i in range(max_redirects):
            self._log(f"会话桥接重定向 {i+1}/{max_redirects}: {current_url[:120]}...")
            if "/api/auth/callback/openai" in current_url and not callback_url:
                callback_url = current_url

            resp = self.session.get(
                current_url,
                headers={
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "referer": "https://chatgpt.com/",
                    "user-agent": ua,
                },
                timeout=25,
                allow_redirects=False,
            )

            # 直接从每一跳响应头 Set-Cookie 抓 session_token（对齐 F12 Network 视角）
            set_cookie_text = self._flatten_set_cookie_headers(resp)
            token_from_header = self._extract_session_token_from_cookie_text(set_cookie_text)
            if token_from_header:
                bridged_header_token = token_from_header
                # 同时写入两种命名兼容，避免库在不同平台下键名差异。
                for name in ("__Secure-next-auth.session-token", "_Secure-next-auth.session-token"):
                    for domain in (".chatgpt.com", "chatgpt.com"):
                        try:
                            self.session.cookies.set(name, token_from_header, domain=domain, path="/")
                        except Exception:
                            continue
                self._log(
                    f"会话桥接命中 Set-Cookie session_token（len={len(token_from_header)}）"
                )

            if resp.status_code not in (301, 302, 303, 307, 308):
                break

            location = str(resp.headers.get("Location") or "").strip()
            if not location:
                break
            current_url = urllib.parse.urljoin(current_url, location)

        if callback_url and not str(current_url or "").startswith("https://chatgpt.com/"):
            try:
                self.session.get(
                    "https://chatgpt.com/",
                    headers={
                        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "referer": current_url,
                        "user-agent": ua,
                    },
                    timeout=20,
                )
            except Exception:
                pass

        self._log(
            f"会话桥接重定向结束: callback={'有' if callback_url else '无'}, "
            f"set_cookie_token={'有' if bool(bridged_header_token) else '无'}, final={current_url[:120]}..."
        )
        return callback_url, current_url

    def _complete_token_exchange(self, result: RegistrationResult, require_login_otp: bool = True) -> bool:
        """在登录态已建立后，补齐 session/access，并尽量获取 OAuth token。"""
        if require_login_otp:
            self._log("等待登录验证码到场，最后这位嘉宾还在路上...")
            self._log("核对登录验证码，验明正身一下...")
            if not self._verify_email_otp_with_retry(stage_label="登录验证码", max_attempts=3):
                result.error_message = "验证码校验失败"
                return False
        else:
            self._log("ABCard 入口链路：跳过二次登录验证码，直接进入 workspace + redirect + auth/session 抓取")

        if require_login_otp and self._try_complete_from_otp_callback(result, stage_label="登录 OTP"):
            self._finalize_result_with_current_tokens(result)
            return True

        self._log("摸一下 Workspace ID，看看该坐哪桌...")
        workspace_id = self._get_workspace_id()
        continue_url = ""
        if workspace_id:
            result.workspace_id = workspace_id

            self._log("选择 Workspace，安排个靠谱座位...")
            workspace_continue = str(self._select_workspace(workspace_id) or "").strip()
            cached_continue = str(self._create_account_continue_url or "").strip()
            continue_url = self._pick_preferred_continue_url(workspace_continue, cached_continue)
            if not continue_url:
                if cached_continue:
                    self._log("workspace/select 未返回 continue_url，改用 create_account 缓存 continue_url", "warning")
                else:
                    result.error_message = "选择 Workspace 失败"
                    return False
        else:
            cached_continue = str(self._create_account_continue_url or "").strip()
            if cached_continue:
                continue_url = cached_continue
                self._log("未获取到 Workspace ID，改用 create_account 缓存 continue_url 继续链路", "warning")
            else:
                result.error_message = "获取 Workspace ID 失败"
                return False

        self._log("顺着重定向面包屑往前走，别跟丢了...")
        callback_url, final_url = self._follow_redirects(continue_url)
        self._log(
            f"重定向链完成，callback={'有' if callback_url else '无'}，final={final_url[:100]}..."
        )
        self._log("重定向链结束，直接请求 /api/auth/session 抓取 session/access...")
        captured = self._capture_auth_session_tokens(result, access_hint=result.access_token, referer=final_url)
        if not captured:
            self._log("直抓未命中，补一次 chatgpt 预热后再抓取...", "warning")
            self._warmup_chatgpt_session()
            captured = self._capture_auth_session_tokens(result, access_hint=result.access_token, referer=final_url)
        final_url_lower = str(final_url or "").lower()
        add_phone_gate = ("auth.openai.com/add-phone" in final_url_lower)

        # ABCard 入口常见失败点：被 add-phone 风控页截断，导致拿不到 callback/session。
        if add_phone_gate and (not callback_url) and (not captured):
            self._log("检测到 auth.openai.com/add-phone 风控页，当前链路未完成 OAuth 回调", "warning")
            if (not require_login_otp) and (not self._is_existing_account):
                self._log("ABCard 入口命中 add-phone，回退原生重登链路再试一次...", "warning")
                login_ready, login_error = self._restart_login_flow()
                if not login_ready:
                    result.error_message = f"ABCard 回退原生链路失败: {login_error}"
                    return False
                return self._complete_token_exchange(result, require_login_otp=True)
            result.error_message = "命中 add-phone 风控页，未获取到 session_token"
            return False

        callback_has_error = bool(
            callback_url and ("error=" in callback_url) and ("code=" not in callback_url)
        )
        if callback_url:
            if callback_has_error:
                self._log(f"回调返回错误参数，跳过 OAuth 回调: {callback_url[:140]}...", "warning")
                if not captured:
                    result.error_message = "OAuth 回调返回 access_denied，且未获取到 auth/session"
                    return False
            else:
                self._log("处理 OAuth 回调，准备把 token 请出来...")
                callback_processed = self._process_oauth_callback_result(
                    result,
                    callback_url,
                    stage_label="会话桥接",
                    referer=final_url,
                )
                captured = callback_processed or captured
                if result.access_token:
                    self._backfill_identity_from_current_session(result, source_label="会话桥接")
                if callback_processed:
                    pass
                elif captured:
                    self._log("OAuth 回调失败，但 session/access 已拿到，继续后续流程", "warning")
                else:
                    result.error_message = "处理 OAuth 回调失败"
                    return False
        else:
            if captured:
                self._log("未拿到 callback_url，但 session/access 已拿到，继续后续流程", "warning")
            else:
                result.error_message = "跟随重定向链失败"
                return False

        result.password = self.password or ""
        result.source = "login" if self._is_existing_account else "register"
        result.device_id = result.device_id or str(self.device_id or "")

        session_cookie = self.session.cookies.get("__Secure-next-auth.session-token")
        if session_cookie:
            self.session_token = session_cookie
            result.session_token = session_cookie
            self._log("Session Token 也捞到了，今天这网没白连")

        if not result.access_token or not result.session_token:
            # 再捞一次，避免某些链路里 session 建立稍慢
            self._capture_auth_session_tokens(result, access_hint=result.access_token)
        if not result.session_token:
            # 对齐 ABCard：尝试走 csrf + signin/openai 的会话桥接。
            self._bootstrap_chatgpt_signin_for_session(result)
        if not result.session_token:
            result.session_token = self._extract_session_token_from_cookie_text(self._dump_session_cookies())
        if not result.device_id:
            result.device_id = str(self.device_id or self.session.cookies.get("oai-did") or "")

        if not result.access_token:
            result.error_message = "未获取到 access_token"
            return False
        if not result.session_token:
            native_register_flow = (self.registration_entry_flow == "native") and (not self._is_existing_account)
            if native_register_flow:
                # 对齐 K:\1\2 备份：原生注册流程里 session_token 不做阻断。
                self._log(
                    "当前链路未拿到 session_token，先保存账号并标记待补会话（可在账号详情/支付页一键补全）",
                    "warning",
                )
            else:
                # 非原生注册入口仍保持强制，避免后续流程不可用。
                if not self._ensure_session_token_strict(result, max_rounds=2):
                    result.error_message = "未获取到 session_token（强制要求）"
                    self._log(
                        "强制模式未拿到 session_token，本次注册判定失败，请检查网络/代理与登录回调链路",
                        "error",
                    )
                    return False

        return True

    def _complete_token_exchange_native_backup(self, result: RegistrationResult) -> bool:
        """
        原生入口对齐备份版收尾链路：
        登录验证码 -> Workspace -> redirect -> OAuth callback -> token 入袋。
        """
        self._log("等待登录验证码到场，最后这位嘉宾还在路上...")
        self._log("核对登录验证码，验明正身一下...")
        login_otp_tried_codes: set[str] = set()
        login_otp_ok = self._verify_email_otp_with_retry(
            stage_label="登录验证码",
            max_attempts=1,
            fetch_timeout=120,
            attempted_codes=login_otp_tried_codes,
        )
        if not login_otp_ok:
            self._log("登录验证码首轮未命中，尝试在当前会话原地重发 OTP 后再校验...", "warning")
            resent = self._send_verification_code(referer="https://auth.openai.com/email-verification")
            if resent:
                login_otp_ok = self._verify_email_otp_with_retry(
                    stage_label="登录验证码(原地重发)",
                    max_attempts=2,
                    fetch_timeout=120,
                    attempted_codes=login_otp_tried_codes,
                )

        if not login_otp_ok:
            self._log("登录验证码仍未命中，尝试重触发登录 OTP 后再校验...", "warning")
            if not self._retrigger_login_otp():
                self._log("重触发登录 OTP 失败，尝试完整重登链路后再校验一次...", "warning")
                login_ready, login_error = self._restart_login_flow()
                if not login_ready:
                    result.error_message = f"登录验证码重触发失败，且完整重登失败: {login_error}"
                    return False
            login_otp_ok = self._verify_email_otp_with_retry(
                stage_label="登录验证码(重发)",
                max_attempts=3,
                fetch_timeout=120,
                attempted_codes=login_otp_tried_codes,
            )
            if not login_otp_ok:
                result.error_message = "验证码校验失败"
                return False

        if self._try_complete_from_otp_callback(result, stage_label="登录 OTP"):
            self._finalize_result_with_current_tokens(result)
            return True

        self._log("摸一下 Workspace ID，看看该坐哪桌...")
        workspace_id = str(self._last_validate_otp_workspace_id or "").strip()
        if workspace_id:
            self._log(f"使用 OTP 返回的 Workspace ID: {workspace_id}")
        if not workspace_id:
            workspace_id = str(self._get_workspace_id() or "").strip()
        if workspace_id:
            result.workspace_id = workspace_id

        continue_url = ""
        workspace_continue = ""
        otp_continue = str(self._last_validate_otp_continue_url or "").strip()
        cached_continue = str(self._create_account_continue_url or "").strip()
        if workspace_id:
            self._log("选择 Workspace，安排个靠谱座位...")
            workspace_continue = str(self._select_workspace(workspace_id) or "").strip()
            continue_url = self._pick_preferred_continue_url(
                workspace_continue,
                otp_continue,
                cached_continue,
            )
            if not continue_url:
                self._log("workspace/select 未返回 continue_url，尝试 OAuth authorize 兜底", "warning")

        if not continue_url:
            oauth_start_url = str(
                (
                    getattr(self.oauth_start, "auth_url", "")
                    or getattr(self.oauth_start, "url", "")
                    if self.oauth_start
                    else ""
                )
                or ""
            ).strip()
            if oauth_start_url:
                continue_url = oauth_start_url
                self._log("使用 OAuth authorize URL 作为兜底 continue_url", "warning")

        if not continue_url and otp_continue:
            continue_url = self._pick_preferred_continue_url(otp_continue, cached_continue)
            if self._is_registration_gate_url(otp_continue):
                self._log("OTP 返回 continue_url 指向注册门页（about-you/add-phone），先顺着门页继续 bridge", "warning")
            else:
                self._log("使用 OTP 返回 continue_url 继续授权链路", "warning")

        if not continue_url and cached_continue:
            continue_url = cached_continue
            if self._is_registration_gate_url(cached_continue):
                self._log("create_account 缓存 continue_url 指向注册门页（about-you/add-phone），先顺着门页继续 bridge", "warning")
            else:
                self._log("使用 create_account 缓存 continue_url 作为兜底", "warning")

        if not continue_url:
            result.error_message = "获取 continue_url 失败"
            return False

        prefer_direct_continue = self._should_try_direct_continue_before_browser(
            continue_url,
            workspace_continue=workspace_continue,
        )
        if prefer_direct_continue and self.registration_browser_first_enabled:
            self._log("OTP 后 workspace/select 返回 fresh continue_url，先直连消费，避免 BrowserClient 预消费授权状态", "warning")

        if self.registration_browser_first_enabled and not prefer_direct_continue:
            browser_captured = self._try_capture_with_root_browser_client(
                result,
                stage_label="原生收尾",
                continue_url=continue_url,
            )
            if browser_captured and result.access_token:
                self._finalize_result_with_current_tokens(result, workspace_hint=workspace_id, source="原生收尾")
                if result.refresh_token:
                    return True
                self._log("原生收尾 BrowserClient 已拿到 session/access，但 refresh_token 仍缺失，继续走 HTTP 兜底", "warning")

        self._log("顺着重定向面包屑往前走，别跟丢了...")
        callback_url, final_url = self._follow_redirects(continue_url)
        if not callback_url:
            self._log("未命中 OAuth 回调，尝试 auth/session 兜底抓取 token...", "warning")
            if self._is_registration_gate_url(final_url):
                self._try_complete_session_from_registration_gate(result, final_url)
            self._capture_auth_session_tokens(result, access_hint=result.access_token, referer=final_url)
            if not result.account_id:
                result.account_id = str(self._create_account_account_id or "").strip()
            if not result.workspace_id:
                result.workspace_id = str(workspace_id or self._create_account_workspace_id or "").strip()
            if not result.refresh_token:
                result.refresh_token = str(self._create_account_refresh_token or "").strip()
            if result.access_token:
                result.password = self.password or ""
                result.source = "login" if self._is_existing_account else "register"
                result.device_id = result.device_id or str(self.device_id or "")
                self._log("未命中 callback，已通过 auth/session 兜底拿到 Access Token，继续完成注册", "warning")
                return True

            # 对新注册账号放宽：账号已创建成功时允许“注册成功、token 待补”
            if (not self._is_existing_account) and self._create_account_account_id:
                result.account_id = result.account_id or str(self._create_account_account_id or "").strip()
                result.workspace_id = result.workspace_id or str(workspace_id or self._create_account_workspace_id or "").strip()
                result.refresh_token = result.refresh_token or str(self._create_account_refresh_token or "").strip()
                result.password = self.password or ""
                result.source = "register"
                result.device_id = result.device_id or str(self.device_id or "")
                self._log("回调链路未命中且未抓到 Access Token，但账号已创建成功；按注册成功收尾（token 待后续补齐）", "warning")
                return True

            result.error_message = "跟随重定向链失败"
            return False

        self._log("处理 OAuth 回调，准备把 token 请出来...")
        callback_processed = self._process_oauth_callback_result(
            result,
            callback_url,
            stage_label="原生入口",
            referer=final_url,
        )
        if not callback_processed:
            if (not self._is_existing_account) and self._create_account_account_id:
                result.account_id = result.account_id or str(self._create_account_account_id or "").strip()
                result.workspace_id = result.workspace_id or str(workspace_id or self._create_account_workspace_id or "").strip()
                result.refresh_token = result.refresh_token or str(self._create_account_refresh_token or "").strip()
                result.password = self.password or ""
                result.source = "register"
                result.device_id = result.device_id or str(self.device_id or "")
                self._log("OAuth 回调处理失败，但账号已创建成功；按注册成功收尾（token 待后续补齐）", "warning")
                return True
            result.error_message = "处理 OAuth 回调失败"
            return False

        self._finalize_result_with_current_tokens(result, workspace_hint=workspace_id, source="原生入口")
        result.password = self.password or ""
        result.source = "login" if self._is_existing_account else "register"
        result.device_id = result.device_id or str(self.device_id or "")

        session_cookie = self.session.cookies.get("__Secure-next-auth.session-token")
        if session_cookie:
            self.session_token = session_cookie
            result.session_token = session_cookie
            self._log("Session Token 也捞到了，今天这网没白连")

        return True

    def _finalize_created_account_without_tokens(
        self,
        result: RegistrationResult,
        *,
        workspace_id: Optional[str] = None,
        warning_message: str,
    ) -> bool:
        """
        新账号已在 create_account 阶段落成，但后续 token 收尾失败时，按“注册成功、token 待补”收尾。
        这样账号密码和邮箱注册历史都会被保存，后续可通过 CSV/续跑流程补齐 token。
        """
        if self._is_existing_account or not self._create_account_completed:
            return False

        result.account_id = str(result.account_id or self._create_account_account_id or "").strip()
        result.workspace_id = str(
            result.workspace_id
            or workspace_id
            or self._last_validate_otp_workspace_id
            or self._create_account_workspace_id
            or ""
        ).strip()
        result.refresh_token = str(result.refresh_token or self._create_account_refresh_token or "").strip()
        result.password = self.password or ""
        result.source = "register"
        result.device_id = result.device_id or str(self.device_id or "")
        result.error_message = ""

        try:
            self._capture_auth_session_tokens(result, access_hint=result.access_token)
        except Exception as e:
            self._log(f"部分成功收尾时补抓会话信息失败: {e}", "warning")

        self._log(warning_message, "warning")
        return True

    def _try_complete_created_account_direct_session(
        self,
        result: RegistrationResult,
        *,
        flow_label: str,
    ) -> bool:
        """
        新账号 create_account 完成后，优先复用其返回的 continue_url/callback。
        成功拿到 ChatGPT session/access 后直接完成注册；失败再回退旧的重登录链路。
        """
        if self._is_existing_account or not self._create_account_completed:
            return False

        create_account_continue = str(self._create_account_continue_url or "").strip()
        otp_continue = str(self._last_validate_otp_continue_url or "").strip()
        preferred_continue = self._pick_preferred_continue_url(
            create_account_continue,
            otp_continue,
        )
        if not preferred_continue:
            self._log(f"{flow_label}快捷路径未拿到 create_account continue_url，回退旧收尾链路", "warning")
            return False

        self._log(f"{flow_label}优先复用 create_account 返回的 continue_url/session...", "warning")
        prefer_direct_continue = self._should_try_direct_continue_before_browser(preferred_continue)
        if prefer_direct_continue and self.registration_browser_first_enabled:
            self._log(
                f"{flow_label}快捷路径命中 live continue_url，先直连消费，避免 BrowserClient 预消费授权状态",
                "warning",
            )

        if self.registration_browser_first_enabled and not prefer_direct_continue:
            browser_captured = self._try_capture_with_root_browser_client(
                result,
                stage_label=f"{flow_label}快捷路径",
                continue_url=preferred_continue,
            )
            if browser_captured:
                self._backfill_identity_from_current_session(result, source_label=f"{flow_label}快捷路径")
                if result.access_token and not result.refresh_token:
                    self._backfill_oauth_tokens_from_authenticated_session(result, source_label=f"{flow_label}快捷路径")
                    self._backfill_identity_from_current_session(result, source_label=f"{flow_label}快捷路径")
                if result.access_token and result.session_token:
                    self._finalize_result_with_current_tokens(result, source=f"{flow_label}快捷路径")
                    if result.refresh_token:
                        return True
                    self._log(f"{flow_label}快捷路径 BrowserClient 已补到 session/access，但 refresh_token 仍缺失，回退旧收尾链路", "warning")
                    return False
                if result.access_token:
                    self._log(f"{flow_label}快捷路径 BrowserClient 只拿到 access_token，继续回退旧收尾链路补全", "warning")

        if self._is_oauth_callback_url(preferred_continue):
            self._process_oauth_callback_result(
                result,
                preferred_continue,
                stage_label=f"{flow_label}快捷路径",
                referer="https://auth.openai.com/about-you",
            )
            if result.access_token:
                self._finalize_result_with_current_tokens(result, source=f"{flow_label}快捷路径")
                if result.refresh_token:
                    return True
                self._log(f"{flow_label}快捷路径 callback 已补到 access/session，但 refresh_token 仍缺失，回退旧收尾链路", "warning")
                return False
            self._log(f"{flow_label}快捷路径 callback 未补到 access_token，回退旧收尾链路", "warning")
            return False

        callback_url, final_url = self._follow_redirects(preferred_continue)
        if callback_url:
            self._process_oauth_callback_result(
                result,
                callback_url,
                stage_label=f"{flow_label}快捷路径",
                referer=final_url,
            )
        else:
            if self._is_registration_gate_url(final_url):
                self._try_complete_session_from_registration_gate(result, final_url)
            self._capture_auth_session_tokens(result, access_hint=result.access_token, referer=final_url)

        if not result.access_token:
            self._log(f"{flow_label}快捷路径未命中 access_token，尝试走 ChatGPT 站内 session bridge...", "warning")
            self._bootstrap_chatgpt_signin_for_session(result)

        if result.access_token and result.session_token:
            self._finalize_result_with_current_tokens(result, source=f"{flow_label}快捷路径")
            if result.refresh_token:
                return True
            self._log(f"{flow_label}快捷路径已拿到 session/access，但 refresh_token 仍缺失，回退旧收尾链路", "warning")
            return False

        if result.access_token:
            self._log(f"{flow_label}快捷路径只拿到 access_token，继续回退旧收尾链路补全", "warning")
            return False

        self._log(f"{flow_label}快捷路径未补到 access_token，回退旧收尾链路", "warning")
        return False

    def _complete_token_exchange_outlook(self, result: RegistrationResult) -> bool:
        """
        Outlook 入口链路（迁移版）：
        对齐 codex-console-main-clean 的收尾流程，
        走「登录 OTP -> Workspace -> OAuth callback」主干，避免 ABCard/native 增强链路干扰。
        同时补齐“第二封验证码”重试链路，避免 Outlook 轮询卡死。
        """
        self._log("等待登录验证码到场，最后这位嘉宾还在路上...")
        self._log("核对登录验证码，验明正身一下...")
        login_otp_tried_codes: set[str] = set()
        login_otp_ok = self._verify_email_otp_with_retry(
            stage_label="登录验证码",
            max_attempts=1,
            fetch_timeout=90,
            attempted_codes=login_otp_tried_codes,
        )
        if not login_otp_ok:
            self._log("登录验证码首轮未命中，先尝试当前会话原地重发 OTP 后再校验...", "warning")
            resent = self._send_verification_code(referer="https://auth.openai.com/email-verification")
            if resent:
                login_otp_ok = self._verify_email_otp_with_retry(
                    stage_label="登录验证码(原地重发)",
                    max_attempts=2,
                    fetch_timeout=90,
                    attempted_codes=login_otp_tried_codes,
                )

        if not login_otp_ok:
            self._log("登录验证码仍未命中，尝试重触发登录 OTP 后再校验...", "warning")
            if not self._retrigger_login_otp():
                self._log("重触发登录 OTP 失败，尝试完整重登链路后再校验一次...", "warning")
                login_ready, login_error = self._restart_login_flow()
                if not login_ready:
                    result.error_message = f"登录验证码重触发失败，且完整重登失败: {login_error}"
                    return False

            login_otp_ok = self._verify_email_otp_with_retry(
                stage_label="登录验证码(重发)",
                max_attempts=3,
                fetch_timeout=120,
                attempted_codes=login_otp_tried_codes,
            )
        if not login_otp_ok:
            result.error_message = "验证码校验失败"
            return False

        if self._try_complete_from_otp_callback(result, stage_label="登录 OTP"):
            self._finalize_result_with_current_tokens(result)
            return True

        self._log("摸一下 Workspace ID，看看该坐哪桌...")
        workspace_id = str(self._last_validate_otp_workspace_id or "").strip()
        if workspace_id:
            self._log(f"使用 OTP 返回的 Workspace ID: {workspace_id}")
        if not workspace_id:
            workspace_id = str(self._get_workspace_id() or "").strip()
        if not workspace_id:
            workspace_id = str(self._last_validate_otp_workspace_id or self._create_account_workspace_id or "").strip()
            if workspace_id:
                self._log(f"Workspace ID（缓存）: {workspace_id}", "warning")

        continue_url = ""
        workspace_continue = ""
        otp_continue = str(self._last_validate_otp_continue_url or "").strip()
        cached_continue = str(self._create_account_continue_url or "").strip()
        if workspace_id:
            result.workspace_id = workspace_id
            self._log("选择 Workspace，安排个靠谱座位...")
            workspace_continue = str(self._select_workspace(workspace_id) or "").strip()
            continue_url = self._pick_preferred_continue_url(
                workspace_continue,
                otp_continue,
                cached_continue,
            )
            if not continue_url:
                self._log("workspace/select 未返回 continue_url，尝试使用缓存 continue_url", "warning")
        else:
            self._log("未获取到 Workspace ID，尝试直接使用缓存 continue_url", "warning")

        if not continue_url:
            continue_url = self._pick_preferred_continue_url(otp_continue, cached_continue)
            if continue_url:
                self._log("使用缓存 continue_url 继续授权链路", "warning")

        if not continue_url:
            if self._finalize_created_account_without_tokens(
                result,
                workspace_id=workspace_id,
                warning_message="未拿到 continue_url，但账号已创建成功；按注册成功收尾（token 待后续补齐）",
            ):
                return True
            result.error_message = "获取 Workspace ID 失败"
            return False

        prefer_direct_continue = self._should_try_direct_continue_before_browser(
            continue_url,
            workspace_continue=workspace_continue,
        )
        if prefer_direct_continue and self.registration_browser_first_enabled:
            self._log("OTP 后 workspace/select 返回 fresh continue_url，先直连消费，避免 BrowserClient 预消费授权状态", "warning")

        if self.registration_browser_first_enabled and not prefer_direct_continue:
            browser_captured = self._try_capture_with_root_browser_client(
                result,
                stage_label="Outlook 收尾",
                continue_url=continue_url,
            )
            if browser_captured and result.access_token:
                self._finalize_result_with_current_tokens(result, workspace_hint=workspace_id, source="Outlook")
                if result.refresh_token:
                    return True
                self._log("Outlook 收尾 BrowserClient 已拿到 session/access，但 refresh_token 仍缺失，继续走 HTTP 兜底", "warning")

        self._log("顺着重定向面包屑往前走，别跟丢了...")
        callback_url, final_url = self._follow_redirects(continue_url)
        if not callback_url:
            if self._is_registration_gate_url(final_url):
                self._try_complete_session_from_registration_gate(result, final_url)
            self._capture_auth_session_tokens(result, access_hint=result.access_token, referer=final_url)
            if result.access_token and result.session_token:
                self._finalize_result_with_current_tokens(result, workspace_hint=workspace_id, source="Outlook")
                if result.refresh_token:
                    return True
                self._log("Outlook 收尾链路已拿到 session/access，但 refresh_token 仍缺失，继续走部分成功兜底", "warning")
            if self._finalize_created_account_without_tokens(
                result,
                workspace_id=workspace_id,
                warning_message="未命中 OAuth 回调，但账号已创建成功；按注册成功收尾（token 待后续补齐）",
            ):
                return True
            result.error_message = "跟随重定向链失败"
            return False

        self._log("处理 OAuth 回调，准备把 token 请出来...")
        callback_processed = self._process_oauth_callback_result(
            result,
            callback_url,
            stage_label="Outlook",
            referer=final_url,
        )
        if not callback_processed:
            if self._finalize_created_account_without_tokens(
                result,
                workspace_id=workspace_id,
                warning_message="OAuth 回调处理失败，但账号已创建成功；按注册成功收尾（token 待后续补齐）",
            ):
                return True
            result.error_message = "处理 OAuth 回调失败"
            return False

        self._finalize_result_with_current_tokens(result, workspace_hint=workspace_id, source="Outlook")
        result.password = self.password or ""
        result.source = "login" if self._is_existing_account else "register"
        result.device_id = result.device_id or str(self.device_id or "")

        session_cookie = self.session.cookies.get("__Secure-next-auth.session-token")
        if session_cookie:
            self.session_token = session_cookie
            result.session_token = session_cookie
            self._log("Session Token 也捞到了，今天这网没白连")

        self._finalize_result_with_current_tokens(result, workspace_hint=workspace_id, source="Outlook")

        if not result.access_token:
            if self._finalize_created_account_without_tokens(
                result,
                workspace_id=workspace_id,
                warning_message="OAuth 回调已完成但未拿到 access_token，账号已创建成功；按注册成功收尾（token 待后续补齐）",
            ):
                return True
            result.error_message = "未获取到 access_token"
            return False

        return True

    def _best_effort_retry_outlook_refresh(self, result: RegistrationResult, max_attempts: int = 1) -> None:
        """
        Outlook 收尾已拿到 session/access 但 refresh_token 缺失时，再完整重登若干轮尝试把链路拉回 consent/callback。
        这是 best-effort，不影响已有部分成功结果。
        """
        attempts = max(int(max_attempts or 0), 0)
        if attempts <= 0:
            return

        for attempt in range(1, attempts + 1):
            if result.refresh_token or self._is_existing_account:
                return

            last_continue = str(self._last_validate_otp_continue_url or "").strip()
            if not self._is_registration_gate_url(last_continue):
                return

            self._log(
                f"Outlook 收尾仍停在 add-phone/about-you，第 {attempt}/{attempts} 次追加重登，尝试拉回 consent/callback...",
                "warning",
            )
            login_ready, login_error = self._restart_login_flow()
            if not login_ready:
                self._log(f"Outlook 追加重登失败，停止 refresh_token 追补: {login_error}", "warning")
                return

            if not self._complete_token_exchange_outlook(result):
                self._log("Outlook 追加重登未完成 token 收尾，保留当前部分成功结果", "warning")
                return

            if result.refresh_token:
                self._log("Outlook 追加重登已补齐 refresh_token", "warning")
                return

    def _ensure_session_token_strict(self, result: RegistrationResult, max_rounds: int = 2) -> bool:
        """
        强制确保 session_token 可用。
        - 先走 auth/session 直抓
        - 再走 ABCard 同款会话桥接
        连续多轮失败则返回 False。
        """
        if result.session_token:
            return True

        rounds = max(int(max_rounds), 1)
        for idx in range(rounds):
            self._log(f"强制补会话 round {idx + 1}/{rounds}：尝试补抓 session_token ...")

            self._warmup_chatgpt_session()
            self._capture_auth_session_tokens(result, access_hint=result.access_token)
            if result.session_token:
                self._log("强制补会话成功：auth/session 已拿到 session_token")
                return True

            self._bootstrap_chatgpt_signin_for_session(result)
            if result.session_token:
                self._log("强制补会话成功：桥接链路已拿到 session_token")
                return True

            fallback_token = self._extract_session_token_from_cookie_text(self._dump_session_cookies())
            if fallback_token:
                result.session_token = fallback_token
                self.session_token = fallback_token
                self._log("强制补会话成功：cookie 文本兜底命中 session_token")
                return True

            self._log("强制补会话本轮未命中 session_token", "warning")

        return False

    def _capture_native_core_tokens(self, result: RegistrationResult) -> bool:
        """
        原生注册入口的轻量 token 抓取：
        - 不做二次登录
        - 不强依赖 session_token
        - 尽量补齐 account/workspace/access/refresh
        """
        try:
            client_id = str(getattr(self.oauth_manager, "client_id", "") or "").strip()
            if client_id:
                self._log(f"原生入口 token 抓取: Client ID: {client_id}")

            if (not result.account_id) and self._create_account_account_id:
                result.account_id = str(self._create_account_account_id or "").strip()
                self._log(f"原生入口 token 抓取: 复用 create_account Account ID: {result.account_id}")
            if (not result.refresh_token) and self._create_account_refresh_token:
                result.refresh_token = str(self._create_account_refresh_token or "").strip()
                self._log("原生入口 token 抓取: 复用 create_account Refresh Token")

            workspace_id = str(result.workspace_id or "").strip()
            if not workspace_id:
                workspace_id = str(self._create_account_workspace_id or "").strip()
            if not workspace_id:
                workspace_id = str(self._get_workspace_id() or "").strip()
            if workspace_id:
                result.workspace_id = workspace_id
                self._log(f"原生入口 token 抓取: Workspace ID: {workspace_id}")
            else:
                self._log("原生入口 token 抓取: 未获取到 Workspace ID", "warning")

            continue_url = ""
            if workspace_id:
                continue_url = str(self._select_workspace(workspace_id) or "").strip()
            if not continue_url:
                cached_continue = str(self._create_account_continue_url or "").strip()
                if cached_continue:
                    continue_url = cached_continue
                    self._log("原生入口 token 抓取: 使用 create_account 缓存 continue_url", "warning")

            callback_url: Optional[str] = None
            final_url = ""
            if continue_url:
                self._log("原生入口 token 抓取: 跟随重定向链获取 OAuth callback...")
                callback_url, final_url = self._follow_redirects(continue_url)
                self._log(
                    f"原生入口 token 抓取: 重定向完成，callback={'有' if callback_url else '无'}，final={str(final_url)[:100]}..."
                )
            else:
                self._log("原生入口 token 抓取: 未获得 continue_url，跳过 callback 交换", "warning")

            callback_has_error = bool(
                callback_url and ("error=" in callback_url) and ("code=" not in callback_url)
            )
            if callback_url and (not callback_has_error):
                if self._process_oauth_callback_result(
                    result,
                    callback_url,
                    stage_label="原生入口 token 抓取",
                    referer=final_url,
                ):
                    self._log(
                        "原生入口 token 抓取结果: "
                        f"account_id={'有' if bool(result.account_id) else '无'}, "
                        f"access={'有' if bool(result.access_token) else '无'}, "
                        f"refresh={'有' if bool(result.refresh_token) else '无'}"
                    )
                else:
                    self._log("原生入口 token 抓取: OAuth 回调处理失败", "warning")
            elif callback_has_error:
                self._log(f"原生入口 token 抓取: callback 含 error，跳过 token 交换: {callback_url[:140]}...", "warning")
            else:
                self._log("原生入口 token 抓取: 未命中 callback_url", "warning")

            # 不走重登，仅轻量探测 auth/session 里的 accessToken（不依赖 session_token）。
            if not result.access_token:
                self._capture_access_token_light(result)
            if (not result.account_id) and result.id_token:
                try:
                    account_info = self.oauth_manager.extract_account_info(result.id_token)
                    result.account_id = str(account_info.get("account_id") or "").strip()
                except Exception:
                    pass
            if (not result.account_id) and result.access_token:
                token_acc = self._extract_account_id_from_access_token(result.access_token)
                if token_acc:
                    result.account_id = token_acc
                    self._log(f"原生入口 token 抓取: 从 access_token 解析 Account ID: {token_acc}")
            if not result.workspace_id:
                try:
                    workspace_id_after = str(self._get_workspace_id() or "").strip()
                    if workspace_id_after:
                        result.workspace_id = workspace_id_after
                        self._log(f"原生入口 token 抓取: 二次获取 Workspace ID 成功: {workspace_id_after}")
                except Exception:
                    pass

            missing = []
            if not result.account_id:
                missing.append("Account ID")
            if not result.workspace_id:
                missing.append("Workspace ID")
            if not result.access_token:
                missing.append("Access Token")
            if not result.refresh_token:
                missing.append("Refresh Token")
            if missing:
                self._log(f"原生入口 token 抓取: 未获取字段 -> {', '.join(missing)}", "warning")

            return bool(result.access_token and result.refresh_token)
        except Exception as e:
            self._log(f"原生入口 token 抓取异常: {e}", "warning")
            return False

    def _capture_access_token_light(self, result: RegistrationResult) -> bool:
        """轻量从 /api/auth/session 抓 accessToken（不依赖 session_token）。"""
        try:
            response = self.session.get(
                "https://chatgpt.com/api/auth/session",
                headers={
                    "accept": "application/json",
                    "referer": "https://chatgpt.com/",
                },
                timeout=20,
            )
            if response.status_code != 200:
                self._log(f"原生入口轻量 auth/session 状态异常: {response.status_code}", "warning")
                return False
            data = response.json() or {}
            session_tokens = self._extract_tokens_from_auth_session_payload(data)
            access_token = str(session_tokens.get("access_token") or "").strip()
            if access_token:
                result.access_token = access_token
                self._log("原生入口轻量 auth/session 命中 Access Token")
                return True
            self._log("原生入口轻量 auth/session 未命中 Access Token", "warning")
            return False
        except Exception as e:
            self._log(f"原生入口轻量 auth/session 异常: {e}", "warning")
            return False

    def _extract_account_id_from_access_token(self, access_token: str) -> str:
        """从 access_token 的 JWT payload 尝试解析 chatgpt_account_id。"""
        try:
            raw = str(access_token or "").strip()
            if raw.count(".") < 2:
                return ""
            payload = raw.split(".")[1]
            import base64
            pad = "=" * ((4 - (len(payload) % 4)) % 4)
            decoded = base64.urlsafe_b64decode((payload + pad).encode("ascii"))
            claims = json.loads(decoded.decode("utf-8"))
            if not isinstance(claims, dict):
                return ""
            auth_claims = claims.get("https://api.openai.com/auth") or {}
            account_id = str(
                auth_claims.get("chatgpt_account_id")
                or claims.get("chatgpt_account_id")
                or ""
            ).strip()
            return account_id
        except Exception:
            return ""

    def _ensure_native_required_tokens(self, result: RegistrationResult) -> bool:
        """
        原生注册入口要求拿齐：
        Account ID / Workspace ID / Client ID / Access Token / Refresh Token
        """
        try:
            if (not result.account_id) and result.id_token:
                try:
                    account_info = self.oauth_manager.extract_account_info(result.id_token)
                    result.account_id = str(account_info.get("account_id") or "").strip()
                except Exception:
                    pass
            if (not result.account_id) and result.access_token:
                result.account_id = self._extract_account_id_from_access_token(result.access_token)

            if not result.workspace_id:
                result.workspace_id = str(self._get_workspace_id() or "").strip()
            if (not result.refresh_token) and self._create_account_refresh_token:
                result.refresh_token = str(self._create_account_refresh_token or "").strip()

            settings = get_settings()
            client_id = str(
                getattr(settings, "openai_client_id", "")
                or getattr(self.oauth_manager, "client_id", "")
                or ""
            ).strip()

            missing = []
            if not result.account_id:
                missing.append("Account ID")
            if not result.workspace_id:
                missing.append("Workspace ID")
            if not client_id:
                missing.append("Client ID")
            if not result.access_token:
                missing.append("Access Token")
            if not result.refresh_token:
                missing.append("Refresh Token")

            if missing:
                self._log(f"原生入口关键参数缺失: {', '.join(missing)}", "error")
                return False

            self._log(
                "原生入口关键参数校验通过: "
                f"Account ID={result.account_id}, Workspace ID={result.workspace_id}, "
                f"Client ID={client_id}, Access=有, Refresh=有"
            )
            return True
        except Exception as e:
            self._log(f"原生入口关键参数校验异常: {e}", "error")
            return False

    def _restart_login_flow(self) -> Tuple[bool, str]:
        """新注册账号完成建号后，重新发起一次登录流程拿 token。"""
        self._token_acquisition_requires_login = True
        self._log("注册这边忙完了，再走一趟登录把 token 请出来，收个尾...")
        self._reset_auth_flow()

        did, sen_token = self._prepare_authorize_flow("重新登录")
        if not did:
            return False, "重新登录时获取 Device ID 失败"
        if not sen_token:
            return False, "重新登录时 Sentinel POW 验证失败"

        login_start_result = self._submit_login_start(did, sen_token)
        if not login_start_result.success:
            return False, f"重新登录提交邮箱失败: {login_start_result.error_message}"
        if self._is_email_otp_page_type(login_start_result.page_type):
            return True, ""
        if not self._is_login_password_page_type(login_start_result.page_type):
            return False, f"重新登录未进入密码页面: {login_start_result.page_type or 'unknown'}"

        password_result = self._submit_login_password()
        if not password_result.success:
            return False, f"重新登录提交密码失败: {password_result.error_message}"
        if not self._is_email_otp_page_type(password_result.page_type):
            return False, f"重新登录未进入验证码页面: {password_result.page_type or 'unknown'}"
        return True, ""

    def _retrigger_login_otp(self) -> bool:
        """
        在“登录验证码”阶段重触发 OTP 发送。
        优先复用登录链路（login_start -> login_password），避免误走注册 OTP 流程。
        """
        try:
            did = str(self.device_id or self.session.cookies.get("oai-did") or "").strip()
            if not did:
                did = str(uuid.uuid4())
                try:
                    self.session.cookies.set("oai-did", did, domain=".chatgpt.com", path="/")
                except Exception:
                    pass
                self.device_id = did

            sen_token = self._check_sentinel(did)
            login_start_result = self._submit_login_start(did, sen_token)
            if not login_start_result.success:
                self._log(
                    f"重触发登录 OTP 失败：提交登录入口失败: {login_start_result.error_message}",
                    "warning",
                )
                return False

            page_type = str(login_start_result.page_type or "").strip()
            if self._is_email_otp_page_type(page_type):
                self._log("重触发登录 OTP 成功：已直达邮箱验证码页")
                return True

            if not self._is_login_password_page_type(page_type):
                self._log(f"重触发登录 OTP 失败：未进入密码页（{page_type or 'unknown'}）", "warning")
                return False

            password_result = self._submit_login_password()
            if not password_result.success:
                self._log(f"重触发登录 OTP 失败：提交登录密码失败: {password_result.error_message}", "warning")
                return False
            if not self._is_email_otp_page_type(password_result.page_type):
                self._log(
                    f"重触发登录 OTP 失败：密码后未进入验证码页（{password_result.page_type or 'unknown'}）",
                    "warning",
                )
                return False

            self._log("重触发登录 OTP 成功：已进入邮箱验证码页")
            return True
        except Exception as e:
            self._log(f"重触发登录 OTP 异常: {e}", "warning")
            return False

    def _register_password(self, did: Optional[str] = None, sen_token: Optional[str] = None) -> Tuple[bool, Optional[str]]:
        """注册密码"""
        try:
            self._last_register_password_error = None
            existing_password = str(self.password or "").strip()
            # 生成密码
            password = self._generate_password()
            self.password = password  # 保存密码到实例变量
            self._log(f"生成密码: {password}")

            # 提交密码注册
            register_body = json.dumps({
                "password": password,
                "username": self.email
            })

            response = self.session.post(
                OPENAI_API_ENDPOINTS["register"],
                headers={
                    "referer": "https://auth.openai.com/create-account/password",
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                data=register_body,
            )

            self._log(f"提交密码状态: {response.status_code}")

            if response.status_code != 200:
                error_text = response.text[:500]
                self._log(f"密码注册失败: {error_text}", "warning")

                # 解析错误信息，判断是否是邮箱已注册
                try:
                    error_json = response.json()
                    error_msg = error_json.get("error", {}).get("message", "")
                    error_code = error_json.get("error", {}).get("code", "")
                    normalized_error_msg = str(error_msg or "").strip()
                    normalized_error_code = str(error_code or "").strip()

                    # 检测邮箱已注册的情况
                    if "already" in normalized_error_msg.lower() or "exists" in normalized_error_msg.lower() or normalized_error_code == "user_exists":
                        self._log(f"邮箱 {self.email} 可能已在 OpenAI 注册过", "error")
                        # 标记此邮箱为已注册状态
                        self._mark_email_as_registered()
                        self._last_register_password_error = "该邮箱可能已在 OpenAI 注册，建议更换邮箱或改走登录流程"
                    elif "failed to register username" in normalized_error_msg.lower():
                        self._last_register_password_error = (
                            "OpenAI 拒绝当前邮箱用户名（可能已占用或触发风控），建议更换邮箱后重试"
                        )
                        if did:
                            self._log("检测到用户名注册失败，尝试登录入口探测邮箱是否已存在...", "warning")
                            try:
                                probe = self._submit_login_start(did, sen_token)
                                if probe.success and (
                                    self._is_login_password_page_type(probe.page_type)
                                    or self._is_email_otp_page_type(probe.page_type)
                                ):
                                    self._log("登录入口探测命中：该邮箱大概率已是 OpenAI 账号", "warning")
                                    self._mark_email_as_registered()
                                    if existing_password:
                                        self.password = existing_password
                                        self._log("已恢复已有账号密码，准备切换到登录链路继续收尾", "warning")
                                        if self._is_email_otp_page_type(probe.page_type):
                                            self._otp_sent_at = time.time()
                                            self._is_existing_account = True
                                        else:
                                            password_result = self._submit_login_password()
                                            if password_result.success and self._is_email_otp_page_type(password_result.page_type):
                                                self._is_existing_account = True
                                            else:
                                                self._last_register_password_error = (
                                                    "该邮箱已存在 OpenAI 账号，但旧密码登录失败；"
                                                    "请检查 CSV 中 OpenAI 账号密码是否正确。"
                                                )

                                        if self._is_existing_account:
                                            self._last_register_password_error = ""
                                            self._log("老账号登录入口已接管，后续继续走登录 OTP 收尾", "warning")
                                    else:
                                        self._last_register_password_error = (
                                            "该邮箱已存在 OpenAI 账号，但当前任务没有旧密码；"
                                            "请补充 OpenAI 账号密码后再走登录续跑。"
                                        )
                            except Exception as probe_error:
                                self._log(f"登录入口探测失败: {probe_error}", "warning")
                    else:
                        self._last_register_password_error = (
                            f"注册密码接口返回异常: {normalized_error_msg or f'HTTP {response.status_code}'}"
                        )
                except Exception:
                    self._last_register_password_error = f"注册密码接口返回异常: HTTP {response.status_code}"

                return False, None

            return True, password

        except Exception as e:
            self._log(f"密码注册失败: {e}", "error")
            self._last_register_password_error = str(e)
            return False, None

    def _mark_email_as_registered(self):
        """标记邮箱为已注册状态（用于后续批量直接跳过）"""
        try:
            with get_db() as db:
                crud.upsert_registered_email(
                    db,
                    email=self.email,
                    provider_type=self.email_service.service_type.value,
                    status="registered_exists_remote",
                    email_service_id=self.email_info.get("service_id") if self.email_info else None,
                    source_task_uuid=self.task_uuid,
                    note="email_already_registered_on_openai",
                )
                self._log(f"已在邮箱注册历史中标记 {self.email} 为已注册状态")
        except Exception as e:
            logger.warning(f"标记邮箱状态失败: {e}")

    def _send_verification_code(self, referer: Optional[str] = None) -> bool:
        """发送验证码"""
        try:
            # 记录发送时间戳
            self._otp_sent_at = time.time()
            send_referer = str(referer or "https://auth.openai.com/create-account/password").strip()

            response = self.session.get(
                OPENAI_API_ENDPOINTS["send_otp"],
                headers={
                    "referer": send_referer,
                    "accept": "application/json",
                },
            )

            self._log(f"验证码发送状态: {response.status_code}")
            return response.status_code == 200

        except Exception as e:
            self._log(f"发送验证码失败: {e}", "error")
            return False

    def _get_verification_code(self, timeout: Optional[int] = None) -> Optional[str]:
        """获取验证码"""
        try:
            mailbox_email = str(self.inbox_email or self.email or "").strip()
            self._log(f"正在等待邮箱 {mailbox_email} 的验证码...")

            email_id = self.email_info.get("service_id") if self.email_info else None
            fetch_timeout = int(timeout) if timeout and int(timeout) > 0 else 120
            code = self.email_service.get_verification_code(
                email=mailbox_email,
                email_id=email_id,
                timeout=fetch_timeout,
                pattern=OTP_CODE_PATTERN,
                otp_sent_at=self._otp_sent_at,
            )

            if code:
                self._log(f"成功获取验证码: {code}")
                return code
            else:
                self._log("等待验证码超时", "error")
                return None

        except Exception as e:
            self._log(f"获取验证码失败: {e}", "error")
            return None

    def _validate_verification_code(self, code: str) -> bool:
        """验证验证码"""
        try:
            self._last_otp_validation_code = str(code or "").strip()
            self._last_otp_validation_status_code = None
            self._last_otp_validation_outcome = ""
            self._last_validate_otp_page_type = ""

            if self.registration_browser_first_enabled:
                browser_outcome = self._try_validate_verification_code_with_browser(code)
                if browser_outcome is not None:
                    self._last_otp_validation_outcome = "success" if browser_outcome else "browser_failed"
                    return bool(browser_outcome)

            code_body = f'{{"code":"{code}"}}'

            response = self.session.post(
                OPENAI_API_ENDPOINTS["validate_otp"],
                headers={
                    "referer": "https://auth.openai.com/email-verification",
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                data=code_body,
            )

            self._log(f"验证码校验状态: {response.status_code}")
            self._last_otp_validation_status_code = int(response.status_code)
            self._last_otp_validation_outcome = "success" if response.status_code == 200 else "http_non_200"
            if response.status_code == 200:
                # 记录 OTP 校验返回中的 continue/workspace 提示，供 native 收尾兜底
                try:
                    import urllib.parse as urlparse
                    payload = response.json() or {}
                    candidates: List[Dict[str, Any]] = []
                    if isinstance(payload, dict):
                        page_payload = payload.get("page")
                        if isinstance(page_payload, dict):
                            page_type = str(page_payload.get("type") or "").strip()
                            if page_type:
                                self._last_validate_otp_page_type = page_type
                                self._log(f"OTP 校验返回页面类型: {page_type}")
                                if self._is_add_phone_page_type(page_type):
                                    self._log("OTP 校验已明确落入 add_phone 门页", "warning")
                        candidates.append(payload)
                        for key in ("data", "result", "next", "payload"):
                            value = payload.get(key)
                            if isinstance(value, dict):
                                candidates.append(value)

                    found_continue = ""
                    found_workspace = ""
                    for item in candidates:
                        if not isinstance(item, dict):
                            continue
                        if not found_workspace:
                            found_workspace = str(
                                item.get("workspace_id")
                                or item.get("workspaceId")
                                or item.get("default_workspace_id")
                                or ((item.get("workspace") or {}).get("id") if isinstance(item.get("workspace"), dict) else "")
                                or ""
                            ).strip()
                        if not found_continue:
                            for key in ("continue_url", "continueUrl", "next_url", "nextUrl", "redirect_url", "redirectUrl", "url"):
                                candidate = str(item.get(key) or "").strip()
                                if not candidate:
                                    continue
                                if candidate.startswith("/"):
                                    candidate = urlparse.urljoin(OPENAI_API_ENDPOINTS["validate_otp"], candidate)
                                found_continue = candidate
                                break
                        if found_workspace and found_continue:
                            break

                    if found_workspace:
                        self._last_validate_otp_workspace_id = found_workspace
                        self._log(f"OTP 校验返回 Workspace ID: {found_workspace}")
                    if found_continue:
                        self._last_validate_otp_continue_url = found_continue
                        self._log(f"OTP 校验返回 continue_url: {found_continue[:100]}...")
                    self._refresh_auth_cookie_workspace_diagnostics(source_label="OTP 校验后")
                except Exception as parse_err:
                    self._log(f"解析 OTP 校验返回信息失败: {parse_err}", "warning")

            return response.status_code == 200

        except Exception as e:
            err_text = str(e or "").lower()
            if (
                "timed out" in err_text
                or "timeout" in err_text
                or "curl: (28)" in err_text
                or "operation timed out" in err_text
            ):
                self._last_otp_validation_outcome = "network_timeout"
            else:
                self._last_otp_validation_outcome = "network_error"
            self._log(f"验证验证码失败: {e}", "error")
            return False

    def _verify_email_otp_with_retry(
        self,
        stage_label: str = "验证码",
        max_attempts: int = 3,
        fetch_timeout: Optional[int] = None,
        attempted_codes: Optional[set[str]] = None,
    ) -> bool:
        """
        获取并校验验证码（带重试）。
        用于规避邮箱里历史验证码导致的 400（第一次取到旧码，第二次取新码）。
        """
        # 每轮验证码阶段开始前，清理上轮 OTP 校验缓存，避免 continue_url/workspace 被旧阶段污染。
        self._last_validate_otp_continue_url = None
        self._last_validate_otp_workspace_id = None
        if attempted_codes is None:
            attempted_codes = set()
        for attempt in range(1, max_attempts + 1):
            code = (
                self._get_verification_code(timeout=fetch_timeout)
                if fetch_timeout
                else self._get_verification_code()
            )
            if not code:
                if attempt < max_attempts:
                    self._log(
                        f"{stage_label}第 {attempt}/{max_attempts} 次未取到验证码，稍后重试...",
                        "warning",
                    )
                    time.sleep(2)
                    continue
                return False

            if code in attempted_codes:
                allow_same_code_retry = (
                    self._last_otp_validation_code == code
                    and self._last_otp_validation_outcome in {"network_timeout", "network_error"}
                )
                if allow_same_code_retry:
                    self._log(
                        f"{stage_label}第 {attempt}/{max_attempts} 次命中重复验证码 {code}，"
                        f"但上次校验为网络异常（{self._last_otp_validation_outcome}），重试同码...",
                        "warning",
                    )
                    if self._validate_verification_code(code):
                        return True
                    if attempt < max_attempts:
                        time.sleep(2)
                        continue
                    return False

                if attempt < max_attempts:
                    self._log(
                        f"{stage_label}第 {attempt}/{max_attempts} 次命中重复验证码 {code}，等待新邮件...",
                        "warning",
                    )
                    time.sleep(2)
                    continue
                return False

            attempted_codes.add(code)

            if self._validate_verification_code(code):
                return True

            if (
                self._last_otp_validation_code == code
                and self._last_otp_validation_outcome in {"network_timeout", "network_error"}
            ):
                if attempt < max_attempts:
                    self._log(
                        f"{stage_label}第 {attempt}/{max_attempts} 次校验遇到网络异常"
                        f"（{self._last_otp_validation_outcome}），优先重试同一验证码...",
                        "warning",
                    )
                    time.sleep(2)
                    if self._validate_verification_code(code):
                        return True
                continue

            if attempt < max_attempts:
                self._log(
                    f"{stage_label}第 {attempt}/{max_attempts} 次校验未通过，疑似旧验证码，自动重试下一封...",
                    "warning",
                )
                time.sleep(2)

        return False

    def _create_user_account(self) -> bool:
        """创建用户账户"""
        try:
            user_info = generate_random_user_info()
            self._log(f"生成用户信息: {user_info['name']}, 生日: {user_info['birthdate']}")
            create_account_body = json.dumps(user_info)
            response = None
            max_attempts = 2

            for attempt in range(1, max_attempts + 1):
                try:
                    response = self.session.post(
                        OPENAI_API_ENDPOINTS["create_account"],
                        headers={
                            "referer": "https://auth.openai.com/about-you",
                            "accept": "application/json",
                            "content-type": "application/json",
                        },
                        data=create_account_body,
                    )
                except cffi_requests.RequestsError as request_error:
                    self._log(f"账户创建请求异常(第 {attempt}/{max_attempts} 次): {request_error}", "warning")
                    if attempt < max_attempts:
                        time.sleep(2 * attempt)
                        continue
                    return False

                self._log(f"账户创建状态: {response.status_code}")

                if response.status_code == 200:
                    break

                retryable_server_error = response.status_code >= 500
                self._log(f"账户创建失败: {response.text[:200]}", "warning")
                if retryable_server_error and attempt < max_attempts:
                    self._log(f"账户创建返回 {response.status_code}，准备重试 (第 {attempt + 1}/{max_attempts} 次)", "warning")
                    time.sleep(2 * attempt)
                    continue
                return False

            if response is None or response.status_code != 200:
                return False

            try:
                data = response.json() or {}
                continue_url = self._pick_preferred_continue_url(
                    data.get("continue_url"),
                    data.get("continueUrl"),
                    data.get("redirect_url"),
                    data.get("redirectUrl"),
                    data.get("url"),
                )
                if continue_url:
                    self._create_account_continue_url = continue_url
                    self._log(f"create_account 返回 continue_url，已缓存: {continue_url[:100]}...")
                account_id = str(
                    data.get("account_id")
                    or data.get("chatgpt_account_id")
                    or (data.get("account") or {}).get("id")
                    or ""
                ).strip()
                if account_id:
                    self._create_account_account_id = account_id
                    self._log(f"create_account 返回 account_id，已缓存: {account_id}")
                workspace_id = str(
                    data.get("workspace_id")
                    or data.get("default_workspace_id")
                    or (data.get("workspace") or {}).get("id")
                    or ""
                ).strip()
                if (not workspace_id) and isinstance(data.get("workspaces"), list) and data.get("workspaces"):
                    workspace_id = str((data.get("workspaces")[0] or {}).get("id") or "").strip()
                if workspace_id:
                    self._create_account_workspace_id = workspace_id
                    self._log(f"create_account 返回 workspace_id，已缓存: {workspace_id}")
                refresh_token = str(data.get("refresh_token") or "").strip()
                if refresh_token:
                    self._create_account_refresh_token = refresh_token
                    self._log("create_account 返回 refresh_token，已缓存")
            except Exception:
                pass

            self._create_account_completed = True

            return True

        except Exception as e:
            self._log(f"创建账户失败: {e}", "error")
            return False

    def _get_workspace_id(self) -> Optional[str]:
        """获取 Workspace ID"""
        try:
            auth_cookie = str(self.session.cookies.get("oai-client-auth-session") or "").strip()
            if not auth_cookie:
                self._log("未能获取到授权 Cookie，尝试从 auth-info 里取 workspace", "warning")

            cookie_candidates: List[Tuple[str, str]] = []
            seen_cookie_names: set[str] = set()

            def _push_cookie(name: str, value: Any) -> None:
                cookie_name = str(name or "").strip()
                cookie_value = str(value or "").strip()
                if not cookie_name or cookie_name in seen_cookie_names or not cookie_value:
                    return
                seen_cookie_names.add(cookie_name)
                cookie_candidates.append((cookie_name, cookie_value))

            _push_cookie("oai-client-auth-session", auth_cookie)
            _push_cookie("oai-client-auth-info", self.session.cookies.get("oai-client-auth-info"))
            try:
                for cookie_name, cookie_value in self.session.cookies.items():
                    lowered = str(cookie_name or "").strip().lower()
                    if any(marker in lowered for marker in ("workspace", "auth", "session")):
                        _push_cookie(cookie_name, cookie_value)
            except Exception:
                pass

            for cookie_name, cookie_value in cookie_candidates:
                workspace_id = self._extract_workspace_id_from_cookie_value(cookie_value)
                if workspace_id:
                    self._log(f"Workspace ID ({cookie_name}): {workspace_id}")
                    return workspace_id

            dump_payload = self._fetch_client_auth_session_dump()
            if isinstance(dump_payload, dict):
                session_payload = dump_payload.get("client_auth_session") or dump_payload.get("session") or dump_payload
                workspace_id = self._extract_workspace_id_from_payload(session_payload)
                if not workspace_id:
                    workspace_id = self._extract_workspace_id_from_text(json.dumps(dump_payload, ensure_ascii=False))
                if workspace_id:
                    self._log(f"Workspace ID (client_auth_session_dump): {workspace_id}")
                    return workspace_id

            # 兜底：复用 create_account 缓存
            cached_workspace = str(self._create_account_workspace_id or "").strip()
            if cached_workspace:
                self._log(f"Workspace ID (create_account缓存): {cached_workspace}")
                return cached_workspace

            self._log("授权 Cookie 里没有 workspace 信息", "warning")
            return None

        except Exception as e:
            self._log(f"获取 Workspace ID 失败: {e}", "error")
            return None

    def _select_workspace(self, workspace_id: str) -> Optional[str]:
        """选择 Workspace"""
        try:
            select_body = f'{{"workspace_id":"{workspace_id}"}}'

            response = self.session.post(
                OPENAI_API_ENDPOINTS["select_workspace"],
                headers={
                    "referer": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
                    "content-type": "application/json",
                    "accept": "application/json",
                },
                data=select_body,
                allow_redirects=False,
            )

            # 兼容 30x：部分环境 continue_url 在 Location 头里。
            location = str(response.headers.get("Location") or "").strip()
            if response.status_code in [301, 302, 303, 307, 308] and location:
                import urllib.parse
                continue_url = urllib.parse.urljoin(OPENAI_API_ENDPOINTS["select_workspace"], location)
                self._log(f"Continue URL (Location): {continue_url[:100]}...")
                return continue_url

            if response.status_code != 200:
                self._log(f"选择 workspace 失败: {response.status_code}", "error")
                self._log(f"响应: {response.text[:200]}", "warning")
                return None

            continue_url = ""
            try:
                continue_url = str((response.json() or {}).get("continue_url") or "").strip()
            except Exception as json_err:
                body_text = str(response.text or "")
                self._log(f"workspace/select 非 JSON 响应，尝试文本兜底解析: {json_err}", "warning")
                # 兜底1：HTML/文本里直接包含 continue_url
                m = re.search(r'"continue_url"\s*:\s*"([^"]+)"', body_text)
                if m:
                    continue_url = str(m.group(1) or "").strip()
                # 兜底2：返回页内含 auth.openai.com/oauth/authorize 链接
                if not continue_url:
                    m2 = re.search(r"https://auth\.openai\.com/[^\s\"'<>]+", body_text)
                    if m2:
                        continue_url = str(m2.group(0) or "").strip()

            if not continue_url:
                if location:
                    import urllib.parse
                    continue_url = urllib.parse.urljoin(OPENAI_API_ENDPOINTS["select_workspace"], location)
                else:
                    self._log("workspace/select 响应里缺少 continue_url", "error")
                    return None

            if continue_url:
                continue_url = continue_url.replace("\\/", "/")
                self._log(f"Continue URL: {continue_url[:100]}...")
                return continue_url

            return None

        except Exception as e:
            self._log(f"选择 Workspace 失败: {e}", "error")
            return None

    def _follow_redirects(self, start_url: str) -> Tuple[Optional[str], str]:
        """手动跟随重定向链，返回 (callback_url, final_url)。"""
        try:
            def _is_oauth_callback(url: str) -> bool:
                try:
                    import urllib.parse as _urlparse

                    parsed = _urlparse.urlparse(url)
                    path = (parsed.path or "").lower()
                    if ("/auth/callback" not in path) and ("/api/auth/callback/openai" not in path):
                        return False
                    query = _urlparse.parse_qs(parsed.query or "", keep_blank_values=True)
                    # 只要带 code 或 error，就认为已经进入回调阶段（避免被本地 503 干扰识别）
                    return bool(query.get("code") or query.get("error"))
                except Exception:
                    return False

            current_url = start_url
            callback_url: Optional[str] = None
            max_redirects = 12

            for i in range(max_redirects):
                self._log(f"重定向 {i+1}/{max_redirects}: {current_url[:100]}...")
                if _is_oauth_callback(current_url) and not callback_url:
                    callback_url = current_url
                    self._log(f"命中回调 URL: {current_url[:120]}...")
                    # 已拿到 callback，不再请求本地 callback 地址，避免 503 干扰后续判断
                    break

                response = self.session.get(
                    current_url,
                    allow_redirects=False,
                    timeout=15
                )

                location = response.headers.get("Location") or ""

                if "/api/auth/callback/openai" in current_url and not callback_url:
                    callback_url = current_url

                # 如果不是重定向状态码，停止
                if response.status_code not in [301, 302, 303, 307, 308]:
                    self._log(f"非重定向状态码: {response.status_code}")
                    break

                if not location:
                    self._log("重定向响应缺少 Location 头")
                    break

                # 构建下一个 URL
                import urllib.parse
                next_url = urllib.parse.urljoin(current_url, location)

                # 命中回调时仅记录，不提前返回；继续跟到底，让 next-auth 充分落 cookie。
                if _is_oauth_callback(next_url) and not callback_url:
                    callback_url = next_url
                    self._log(f"找到回调 URL: {next_url[:100]}...")
                    current_url = next_url
                    break

                current_url = next_url

            # 对齐 ABCard：补打一跳 chatgpt 首页，确保 next-auth cookie 完整落地。
            try:
                if not current_url.rstrip("/").endswith("chatgpt.com"):
                    self.session.get(
                        "https://chatgpt.com/",
                        headers={
                            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                            "referer": current_url,
                            "user-agent": (
                                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                            ),
                        },
                        timeout=20,
                    )
            except Exception as home_err:
                self._log(f"重定向结束后首页补跳异常: {home_err}", "warning")

            if not callback_url:
                self._log("未能在重定向链中找到回调 URL", "warning")
            return callback_url, current_url

        except Exception as e:
            self._log(f"跟随重定向失败: {e}", "error")
            return None, start_url

    def _handle_oauth_callback(self, callback_url: str) -> Optional[Dict[str, Any]]:
        """处理 OAuth 回调"""
        def _is_retryable_callback_error(error: Exception) -> bool:
            message = str(error or "").lower()
            if not message:
                return False
            if "network error" in message:
                return True
            if "timed out" in message or "timeout" in message or "curl: (28)" in message:
                return True
            return any(f"token exchange failed: {status}" in message for status in ("500", "502", "503", "504"))

        if not self.oauth_start:
            self._log("OAuth 流程未初始化", "error")
            return None

        settings = get_settings()
        max_attempts = max(int(getattr(settings, "registration_token_exchange_max_retries", 3) or 3), 1)
        last_error: Optional[Exception] = None

        for attempt in range(1, max_attempts + 1):
            try:
                if attempt == 1:
                    self._log("处理 OAuth 回调，最后一哆嗦，稳住别抖...")
                else:
                    self._log(f"处理 OAuth 回调重试中 (第 {attempt}/{max_attempts} 次)...", "warning")

                token_info = self.oauth_manager.handle_callback(
                    callback_url=callback_url,
                    expected_state=self.oauth_start.state,
                    code_verifier=self.oauth_start.code_verifier
                )

                self._log("OAuth 授权成功，通关文牒到手")
                return token_info

            except Exception as e:
                last_error = e
                if attempt < max_attempts and _is_retryable_callback_error(e):
                    self._log(f"处理 OAuth 回调遇到瞬时错误，准备重试: {e}", "warning")
                    time.sleep(2 * attempt)
                    continue
                break

        self._log(f"处理 OAuth 回调失败: {last_error}", "error")
        return None

    def run(self) -> RegistrationResult:
        """
        执行完整的注册流程

        支持已注册账号自动登录：
        - 如果检测到邮箱已注册，自动切换到登录流程
        - 已注册账号跳过：设置密码、发送验证码、创建用户账户
        - 共用步骤：获取验证码、验证验证码、Workspace 和 OAuth 回调

        Returns:
            RegistrationResult: 注册结果
        """
        result = RegistrationResult(success=False, logs=self.logs)

        try:
            self._is_existing_account = False
            self._token_acquisition_requires_login = False
            self._otp_sent_at = None
            self._create_account_continue_url = None
            self._create_account_workspace_id = None
            self._create_account_account_id = None
            self._create_account_refresh_token = None
            self._create_account_completed = False
            self._last_validate_otp_continue_url = None
            self._last_validate_otp_workspace_id = None
            self._last_email_otp_start_url = None
            self._last_validate_otp_page_type = ""
            self._last_auth_cookie_has_workspace = False
            self._last_auth_cookie_workspace_id = ""

            self._log("=" * 60)
            self._log("注册流程启动，开始替你敲门")
            self._log("=" * 60)
            self._log(f"注册入口链路配置: {self.registration_entry_flow}")
            configured_entry_flow = self.registration_entry_flow
            service_type_raw = getattr(self.email_service, "service_type", "")
            service_type_value = str(getattr(service_type_raw, "value", service_type_raw) or "").strip().lower()
            effective_entry_flow = configured_entry_flow
            if service_type_value == "outlook":
                self._log("检测到 Outlook 邮箱，自动使用 Outlook 入口链路（无需在设置中选择）")
                effective_entry_flow = "outlook"

            # 1. 检查 IP 地理位置
            self._log("1. 先看看这条网络从哪儿来，别一开局就站错片场...")
            ip_ok, location = self._check_ip_location()
            if not ip_ok:
                result.error_message = f"IP 地理位置不支持: {location}"
                self._log(f"IP 检查失败: {location}", "error")
                return result

            self._log(f"IP 位置: {location or '未知（检测接口未返回地区，已继续）'}")

            # 2. 创建邮箱
            self._log("2. 开个新邮箱，准备收信...")
            if not self._create_email():
                result.error_message = "创建邮箱失败"
                return result

            result.email = self.email
            browser_reference_completed = False

            if effective_entry_flow == "outlook":
                browser_reference_completed = self._try_run_outlook_browser_reference(result)
                if browser_reference_completed:
                    self._log("注册入口链路: Outlook Browser FSM（继承参考版主流程）", "warning")

            if not browser_reference_completed:
                # 3. 准备首轮授权流程
                did, sen_token = self._prepare_authorize_flow("首次授权")
                if not did:
                    result.error_message = "获取 Device ID 失败"
                    return result
                result.device_id = did
                if not sen_token:
                    result.error_message = "Sentinel POW 验证失败"
                    return result

                # 4. 提交注册入口邮箱
                self._log("4. 递上邮箱，看看 OpenAI 这球怎么接...")
                signup_result = self._submit_signup_form(did, sen_token)
                if not signup_result.success:
                    result.error_message = f"提交注册表单失败: {signup_result.error_message}"
                    return result

                if self._is_existing_account:
                    self._log("检测到这是老朋友账号，直接切去登录拿 token，不走弯路")
                else:
                    self._log("5. 设置密码，别让小偷偷笑...")
                    password_ok, _ = self._register_password(did, sen_token)
                    if not password_ok:
                        if self._is_existing_account:
                            self._log("注册密码阶段确认该邮箱已存在，切换到登录链路继续收尾", "warning")
                        else:
                            result.error_message = self._last_register_password_error or "注册密码失败"
                            return result

                    if not self._is_existing_account:
                        self._log("6. 催一下注册验证码出门，邮差该冲刺了...")
                        if not self._send_verification_code():
                            result.error_message = "发送验证码失败"
                            return result

                        self._log("7. 等验证码飞来，邮箱请注意查收...")
                        self._log("8. 对一下验证码，看看是不是本人...")
                        if not self._verify_email_otp_with_retry(stage_label="注册验证码", max_attempts=3):
                            result.error_message = "验证验证码失败"
                            return result

                        self._log("9. 给账号办个正式户口，名字写档案里...")
                        if not self._create_user_account():
                            result.error_message = "创建用户账户失败"
                            return result

                        direct_session_completed = False
                        if effective_entry_flow in {"native", "outlook"}:
                            direct_session_completed = self._try_complete_created_account_direct_session(
                                result,
                                flow_label=("Outlook" if effective_entry_flow == "outlook" else "原生注册"),
                            )

                        if effective_entry_flow in {"native", "outlook"}:
                            if direct_session_completed:
                                if effective_entry_flow == "outlook":
                                    self._log("注册入口链路: Outlook（优先复用 create_account 会话完成收尾）")
                                else:
                                    self._log("注册入口链路: native（优先复用 create_account 会话完成收尾）")
                            else:
                                login_ready, login_error = self._restart_login_flow()
                                if not login_ready:
                                    result.error_message = login_error
                                    return result
                                if effective_entry_flow == "outlook":
                                    self._log("注册入口链路: Outlook（迁移版，按朋友版 Outlook 主流程收尾）")
                        else:
                            self._log("注册入口链路: ABCard（新账号不重登，直接抓取会话）")

            if effective_entry_flow == "native":
                needs_native_completion = not all(
                    [
                        bool(result.account_id),
                        bool(result.workspace_id),
                        bool(result.access_token),
                        bool(result.refresh_token),
                    ]
                )
                if needs_native_completion and not self._complete_token_exchange_native_backup(result):
                    return result
            elif effective_entry_flow == "outlook":
                needs_outlook_completion = not all(
                    [
                        bool(result.account_id),
                        bool(result.workspace_id),
                        bool(result.access_token),
                        bool(result.refresh_token),
                    ]
                )
                if needs_outlook_completion:
                    if browser_reference_completed:
                        self._backfill_identity_from_current_session(result, source_label="Outlook Browser FSM")
                        self._backfill_oauth_tokens_from_authenticated_session(result, source_label="Outlook Browser FSM")
                        self._finalize_result_with_current_tokens(result, workspace_hint=result.workspace_id, source="Outlook")
                        if not result.access_token:
                            result.error_message = "Outlook Browser FSM 未获取到 access_token"
                            return result
                        if not result.refresh_token:
                            self._log("Outlook Browser FSM 已拿到 session/access，但 refresh_token 仍缺失，按部分成功继续", "warning")
                    elif not self._complete_token_exchange_outlook(result):
                        return result
                    elif not result.refresh_token:
                        self._best_effort_retry_outlook_refresh(result, max_attempts=1)
            else:
                use_abcard_entry = (effective_entry_flow == "abcard") and (not self._is_existing_account)
                if not self._complete_token_exchange(result, require_login_otp=not use_abcard_entry):
                    return result

            # 10. 完成
            self._log("=" * 60)
            if self._is_existing_account:
                self._log("登录成功，老朋友顺利回家")
            else:
                self._log("注册成功，账号已经稳稳落地，可以开香槟了")
            self._log(f"邮箱: {result.email}")
            self._log(f"Device ID: {result.device_id or '-'}")
            self._log(f"Account ID: {result.account_id}")
            self._log(f"Workspace ID: {result.workspace_id}")
            self._log("=" * 60)

            result.success = True
            settings = get_settings()
            client_id = str(getattr(settings, "openai_client_id", "") or getattr(self.oauth_manager, "client_id", "") or "").strip()
            result.metadata = {
                "email_service": self.email_service.service_type.value,
                "proxy_used": self.proxy_url,
                "registered_at": datetime.now().isoformat(),
                "is_existing_account": self._is_existing_account,
                "token_acquired_via_relogin": self._token_acquisition_requires_login,
                "client_id": client_id,
                "device_id": result.device_id,
                "has_session_token": bool(result.session_token),
                "has_access_token": bool(result.access_token),
                "has_refresh_token": bool(result.refresh_token),
                "registration_entry_flow": configured_entry_flow,
                "registration_entry_flow_effective": effective_entry_flow,
                "registration_browser_first_enabled": self.registration_browser_first_enabled,
                "registration_browser_headless": self.registration_browser_headless,
                "registration_browser_persistent_profile_dir": self.registration_browser_persistent_profile_dir,
                "last_validate_otp_page_type": str(self._last_validate_otp_page_type or ""),
                "last_validate_otp_continue_url": str(self._last_validate_otp_continue_url or ""),
                "phone_required_gate_seen": self._is_add_phone_page_type(self._last_validate_otp_page_type),
                "auth_cookie_has_workspace": bool(self._last_auth_cookie_has_workspace),
                "auth_cookie_workspace_id": str(self._last_auth_cookie_workspace_id or ""),
                # 对齐 K:\1\2：原生入口允许无 session_token 成功，但会标记待补。
                "session_token_pending": (effective_entry_flow == "native") and (not bool(result.session_token)),
            }

            return result

        except Exception as e:
            self._log(f"注册过程中发生未预期错误: {e}", "error")
            result.error_message = str(e)
            return result

    def save_to_database(self, result: RegistrationResult) -> bool:
        """
        保存注册结果到数据库

        Args:
            result: 注册结果

        Returns:
            是否保存成功
        """
        if not result.success:
            return False

        try:
            # 获取默认 client_id
            settings = get_settings()
            extra_data = dict(result.metadata or {})
            recovery_payload = self._build_outlook_recovery_payload()
            if recovery_payload:
                extra_data[ACCOUNT_OUTLOOK_RECOVERY_KEY] = recovery_payload

            with get_db() as db:
                existing_account = crud.get_account_by_email(db, result.email)
                cookies_text = self._dump_session_cookies()

                if existing_account:
                    merged_extra_data = dict(existing_account.extra_data or {})
                    merged_extra_data.update(extra_data)
                    account = crud.update_account(
                        db,
                        existing_account.id,
                        password=str(result.password or "").strip() or None,
                        client_id=str(settings.openai_client_id or "").strip() or None,
                        session_token=str(result.session_token or "").strip() or None,
                        cookies=str(cookies_text or "").strip() or None,
                        email_service=self.email_service.service_type.value,
                        email_service_id=self.email_info.get("service_id") if self.email_info else None,
                        account_id=str(result.account_id or "").strip() or None,
                        workspace_id=str(result.workspace_id or "").strip() or None,
                        access_token=str(result.access_token or "").strip() or None,
                        refresh_token=str(result.refresh_token or "").strip() or None,
                        id_token=str(result.id_token or "").strip() or None,
                        proxy_used=str(self.proxy_url or "").strip() or None,
                        extra_data=merged_extra_data,
                        source=str(result.source or "").strip() or None,
                        status="active",
                    )
                    self._log(f"账户已按邮箱补全更新，落袋为安，ID: {account.id}")
                else:
                    account = crud.create_account(
                        db,
                        email=result.email,
                        password=result.password,
                        client_id=settings.openai_client_id,
                        session_token=result.session_token,
                        cookies=cookies_text,
                        email_service=self.email_service.service_type.value,
                        email_service_id=self.email_info.get("service_id") if self.email_info else None,
                        account_id=result.account_id,
                        workspace_id=result.workspace_id,
                        access_token=result.access_token,
                        refresh_token=result.refresh_token,
                        id_token=result.id_token,
                        proxy_used=self.proxy_url,
                        extra_data=extra_data,
                        source=result.source
                    )
                    self._log(f"账户已存进数据库，落袋为安，ID: {account.id}")
                crud.upsert_registered_email(
                    db,
                    email=result.email,
                    provider_type=self.email_service.service_type.value,
                    status="registered_success",
                    email_service_id=self.email_info.get("service_id") if self.email_info else None,
                    account_id=account.id,
                    source_task_uuid=self.task_uuid,
                    note="saved_from_registration_result",
                )

                return True

        except Exception as e:
            self._log(f"保存到数据库失败: {e}", "error")
            return False

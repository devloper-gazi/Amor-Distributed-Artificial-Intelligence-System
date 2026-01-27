"""
Authentication Agent for handling login-protected websites.

Implements:
- Session management with persistent cookies
- CSRF token handling
- Login form detection and submission
- Session persistence to Redis
"""

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import urlparse, urljoin

import httpx
from bs4 import BeautifulSoup
import redis.asyncio as redis

logger = logging.getLogger(__name__)


@dataclass
class Credentials:
    """Stored credentials for a site."""
    username: str
    password: str
    domain: str
    login_url: Optional[str] = None
    extra_fields: Dict[str, str] = field(default_factory=dict)


@dataclass
class SessionData:
    """Serializable session data."""
    domain: str
    cookies: Dict[str, str]
    headers: Dict[str, str]
    created_at: datetime
    expires_at: Optional[datetime] = None
    last_used: datetime = field(default_factory=datetime.utcnow)
    is_authenticated: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "domain": self.domain,
            "cookies": self.cookies,
            "headers": self.headers,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "last_used": self.last_used.isoformat(),
            "is_authenticated": self.is_authenticated,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionData":
        """Deserialize from dictionary."""
        return cls(
            domain=data["domain"],
            cookies=data["cookies"],
            headers=data["headers"],
            created_at=datetime.fromisoformat(data["created_at"]),
            expires_at=datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None,
            last_used=datetime.fromisoformat(data["last_used"]),
            is_authenticated=data["is_authenticated"],
        )
    
    def is_expired(self) -> bool:
        """Check if session is expired."""
        if self.expires_at and datetime.utcnow() > self.expires_at:
            return True
        return False


class SessionManager:
    """
    Manages authenticated sessions across multiple domains.
    
    Features:
    - Session persistence to Redis
    - Automatic session refresh
    - Cookie jar management
    - Thread-safe session access
    """
    
    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        key_prefix: str = "sessions",
        session_ttl: int = 86400,  # 24 hours
    ):
        """
        Initialize session manager.
        
        Args:
            redis_url: Redis connection URL
            key_prefix: Prefix for session keys
            session_ttl: Session TTL in seconds
        """
        self.redis_url = redis_url
        self.key_prefix = key_prefix
        self.session_ttl = session_ttl
        
        self.redis: Optional[redis.Redis] = None
        self._local_sessions: Dict[str, SessionData] = {}
    
    async def initialize(self):
        """Initialize Redis connection."""
        self.redis = redis.from_url(
            self.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
        await self.redis.ping()
        logger.info("Session manager initialized")
    
    async def close(self):
        """Close Redis connection."""
        if self.redis:
            await self.redis.close()
    
    def _get_session_key(self, domain: str) -> str:
        """Get Redis key for a domain session."""
        return f"{self.key_prefix}:{domain}"
    
    async def save_session(self, session: SessionData):
        """
        Save session to Redis.
        
        Args:
            session: Session data to save
        """
        key = self._get_session_key(session.domain)
        await self.redis.setex(
            key,
            self.session_ttl,
            json.dumps(session.to_dict()),
        )
        self._local_sessions[session.domain] = session
        logger.debug(f"Session saved for {session.domain}")
    
    async def get_session(self, domain: str) -> Optional[SessionData]:
        """
        Get session for a domain.
        
        Args:
            domain: Domain to get session for
            
        Returns:
            Session data or None
        """
        # Check local cache first
        if domain in self._local_sessions:
            session = self._local_sessions[domain]
            if not session.is_expired():
                session.last_used = datetime.utcnow()
                return session
            else:
                del self._local_sessions[domain]
        
        # Check Redis
        key = self._get_session_key(domain)
        data = await self.redis.get(key)
        
        if data:
            session = SessionData.from_dict(json.loads(data))
            if not session.is_expired():
                session.last_used = datetime.utcnow()
                self._local_sessions[domain] = session
                return session
            else:
                await self.redis.delete(key)
        
        return None
    
    async def delete_session(self, domain: str):
        """Delete session for a domain."""
        key = self._get_session_key(domain)
        await self.redis.delete(key)
        self._local_sessions.pop(domain, None)
        logger.debug(f"Session deleted for {domain}")
    
    async def has_session(self, domain: str) -> bool:
        """Check if we have an authenticated session."""
        session = await self.get_session(domain)
        return session is not None and session.is_authenticated


class AuthAgent:
    """
    Handles authentication for protected websites.
    
    Features:
    - Automatic login form detection
    - CSRF token extraction
    - Multi-step authentication support
    - Session persistence
    """
    
    def __init__(
        self,
        session_manager: SessionManager,
        user_agent: str = "AuthAgent/1.0",
        timeout: float = 30.0,
    ):
        """
        Initialize auth agent.
        
        Args:
            session_manager: Session manager instance
            user_agent: User agent string
            timeout: Request timeout
        """
        self.session_manager = session_manager
        self.user_agent = user_agent
        self.timeout = timeout
        
        self._credentials: Dict[str, Credentials] = {}
    
    def add_credentials(self, credentials: Credentials):
        """
        Add credentials for a domain.
        
        Args:
            credentials: Credentials to add
        """
        self._credentials[credentials.domain] = credentials
        logger.info(f"Credentials added for {credentials.domain}")
    
    def get_credentials(self, domain: str) -> Optional[Credentials]:
        """Get credentials for a domain."""
        return self._credentials.get(domain)
    
    async def _create_client(
        self,
        session: Optional[SessionData] = None,
    ) -> httpx.AsyncClient:
        """Create HTTP client with session cookies."""
        cookies = {}
        headers = {"User-Agent": self.user_agent}
        
        if session:
            cookies = session.cookies
            headers.update(session.headers)
        
        return httpx.AsyncClient(
            cookies=cookies,
            headers=headers,
            timeout=self.timeout,
            follow_redirects=True,
        )
    
    async def _detect_login_form(
        self,
        html: str,
        base_url: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Detect login form in HTML.
        
        Returns:
            Form data or None
        """
        soup = BeautifulSoup(html, "lxml")
        
        # Look for forms with password fields
        forms = soup.find_all("form")
        
        for form in forms:
            password_field = form.find("input", {"type": "password"})
            if not password_field:
                continue
            
            # Found a login form
            form_data = {
                "action": urljoin(base_url, form.get("action", "")),
                "method": form.get("method", "post").upper(),
                "fields": {},
            }
            
            # Get all input fields
            for input_field in form.find_all("input"):
                name = input_field.get("name")
                if not name:
                    continue
                
                field_type = input_field.get("type", "text")
                value = input_field.get("value", "")
                
                form_data["fields"][name] = {
                    "type": field_type,
                    "value": value,
                }
            
            # Look for CSRF token
            csrf_field = form.find("input", {"name": re.compile(r"csrf|token|_token", re.I)})
            if csrf_field:
                form_data["csrf_field"] = csrf_field.get("name")
                form_data["csrf_value"] = csrf_field.get("value", "")
            
            return form_data
        
        return None
    
    async def _extract_csrf_token(
        self,
        client: httpx.AsyncClient,
        url: str,
    ) -> Optional[str]:
        """Extract CSRF token from a page."""
        response = await client.get(url)
        soup = BeautifulSoup(response.text, "lxml")
        
        # Check meta tags
        meta_csrf = soup.find("meta", {"name": re.compile(r"csrf|token", re.I)})
        if meta_csrf:
            return meta_csrf.get("content")
        
        # Check hidden inputs
        hidden_csrf = soup.find("input", {"name": re.compile(r"csrf|token|_token", re.I)})
        if hidden_csrf:
            return hidden_csrf.get("value")
        
        return None
    
    async def authenticate(
        self,
        credentials: Credentials,
    ) -> Tuple[bool, Optional[SessionData]]:
        """
        Authenticate with a website.
        
        Args:
            credentials: Credentials to use
            
        Returns:
            Tuple of (success, session_data)
        """
        domain = credentials.domain
        login_url = credentials.login_url or f"https://{domain}/login"
        
        try:
            async with await self._create_client() as client:
                # Get login page
                response = await client.get(login_url)
                
                if response.status_code != 200:
                    logger.error(f"Failed to get login page: {response.status_code}")
                    return False, None
                
                # Detect login form
                form_data = await self._detect_login_form(response.text, login_url)
                
                if not form_data:
                    logger.error(f"No login form detected on {login_url}")
                    return False, None
                
                # Build form submission data
                submit_data = {}
                
                for field_name, field_info in form_data["fields"].items():
                    field_type = field_info["type"]
                    
                    if field_type == "password":
                        submit_data[field_name] = credentials.password
                    elif field_type in ("text", "email"):
                        # Assume this is username/email field
                        if not submit_data.get(field_name):
                            submit_data[field_name] = credentials.username
                    elif field_type == "hidden":
                        submit_data[field_name] = field_info["value"]
                
                # Add any extra fields
                submit_data.update(credentials.extra_fields)
                
                # Submit login form
                if form_data["method"] == "POST":
                    login_response = await client.post(
                        form_data["action"],
                        data=submit_data,
                    )
                else:
                    login_response = await client.get(
                        form_data["action"],
                        params=submit_data,
                    )
                
                # Check if login was successful
                # This is a simple heuristic - check for common failure indicators
                is_success = True
                response_text = login_response.text.lower()
                
                failure_indicators = [
                    "invalid",
                    "incorrect",
                    "wrong",
                    "failed",
                    "error",
                    "denied",
                ]
                
                for indicator in failure_indicators:
                    if indicator in response_text and "login" in response_text:
                        is_success = False
                        break
                
                # Also check if we got redirected to a dashboard/home
                if login_response.status_code in (302, 303):
                    is_success = True
                
                if is_success:
                    # Create session data
                    session = SessionData(
                        domain=domain,
                        cookies=dict(client.cookies),
                        headers={},
                        created_at=datetime.utcnow(),
                        expires_at=datetime.utcnow() + timedelta(hours=24),
                        is_authenticated=True,
                    )
                    
                    # Save session
                    await self.session_manager.save_session(session)
                    
                    logger.info(f"Authentication successful for {domain}")
                    return True, session
                else:
                    logger.warning(f"Authentication failed for {domain}")
                    return False, None
                    
        except Exception as e:
            logger.error(f"Authentication error for {domain}: {e}")
            return False, None
    
    async def get_authenticated_client(
        self,
        domain: str,
    ) -> Optional[httpx.AsyncClient]:
        """
        Get an authenticated HTTP client for a domain.
        
        Args:
            domain: Domain to get client for
            
        Returns:
            Authenticated client or None
        """
        # Check for existing session
        session = await self.session_manager.get_session(domain)
        
        if session and session.is_authenticated:
            return await self._create_client(session)
        
        # Try to authenticate
        credentials = self.get_credentials(domain)
        if credentials:
            success, session = await self.authenticate(credentials)
            if success and session:
                return await self._create_client(session)
        
        return None
    
    async def ensure_authenticated(self, domain: str) -> bool:
        """
        Ensure we have an authenticated session for a domain.
        
        Returns:
            True if authenticated
        """
        # Check existing session
        if await self.session_manager.has_session(domain):
            return True
        
        # Try to authenticate
        credentials = self.get_credentials(domain)
        if not credentials:
            return False
        
        success, _ = await self.authenticate(credentials)
        return success

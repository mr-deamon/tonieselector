from pathlib import Path
import asyncio
import base64
import hashlib
import mimetypes
import re
import secrets
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx

from app.config import settings


class MyToniesClient:
    _cached_token: str | None = None
    _cached_token_expires_at: float = 0.0
    _token_lock = asyncio.Lock()

    async def list_figures(self) -> list[dict[str, str]]:
        households = await self._get_households()

        figures: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        for household in households:
            if not isinstance(household, dict):
                continue
            creative_tonies = household.get("creativeTonies")
            if not isinstance(creative_tonies, list):
                continue
            for tonie in creative_tonies:
                if not isinstance(tonie, dict):
                    continue
                figure_id = tonie.get("id")
                if not figure_id:
                    continue
                figure_id_text = str(figure_id)
                if figure_id_text in seen_ids:
                    continue
                seen_ids.add(figure_id_text)
                label = tonie.get("name") or figure_id_text
                image_url = next(
                    (
                        str(value)
                        for key in (
                            "imageUrl",
                            "image_url",
                            "image",
                            "avatarUrl",
                            "avatar_url",
                            "picture",
                            "pictureUrl",
                            "picture_url",
                            "iconUrl",
                            "icon_url",
                        )
                        if (value := tonie.get(key))
                    ),
                    "",
                )
                figure = {"id": figure_id_text, "name": str(label)}
                if image_url:
                    figure["image_url"] = image_url
                figures.append(figure)

        return figures

    async def upload_album_files(self, figure_id: str, files: list[Path]) -> dict:
        if settings.my_tonies_mock_upload:
            return {
                "status": "mocked",
                "figure_id": figure_id,
                "uploaded_files": [str(path) for path in files],
            }

        if not settings.my_tonies_base_url:
            raise RuntimeError("MY_TONIES_BASE_URL is required (example: https://api.prod.tcs.toys/v2)")

        uploaded: list[str] = []
        uploaded_file_ids: list[str] = []
        chapter_items: list[dict[str, str]] = []
        household_id = await self._get_household_id_for_figure(figure_id)
        headers = {
            **(await self._build_auth_headers()),
            "Accept": "*/*",
        }

        async with httpx.AsyncClient(timeout=120.0) as client:
            await self._clear_chapters(client, headers, household_id, figure_id)

            for audio_file in files:
                file_id = await self._upload_file_via_presigned_form(client, headers, audio_file)
                uploaded_file_ids.append(file_id)
                chapter_title = audio_file.stem.strip() or audio_file.name
                chapter_items.append({"file": file_id, "title": chapter_title})
                uploaded.append(str(audio_file))

            await self._apply_chapters(client, headers, household_id, figure_id, chapter_items)

        return {
            "status": "uploaded",
            "figure_id": figure_id,
            "household_id": household_id,
            "uploaded_files": uploaded,
            "uploaded_file_ids": uploaded_file_ids,
        }

    async def _get_households(self) -> list[dict]:
        headers = await self._build_auth_headers()
        graphql_payload = {
            "query": (
                "{\n"
                "  households {\n"
                "    id\n"
                "    name\n"
                "    creativeTonies {\n"
                "      id\n"
                "      name\n"
                "      imageUrl\n"
                "    }\n"
                "  }\n"
                "}"
            ),
            "variables": {},
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                settings.my_tonies_graphql_url,
                headers={**headers, "Content-Type": "application/json", "Accept": "*/*"},
                json=graphql_payload,
            )
            response.raise_for_status()

        payload = response.json()
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        if not isinstance(data, dict):
            return []
        households = data.get("households")
        if not isinstance(households, list):
            return []
        return households

    async def _get_household_id_for_figure(self, figure_id: str) -> str:
        households = await self._get_households()
        for household in households:
            if not isinstance(household, dict):
                continue
            household_id = household.get("id")
            if not household_id:
                continue
            creative_tonies = household.get("creativeTonies")
            if not isinstance(creative_tonies, list):
                continue
            for tonie in creative_tonies:
                if isinstance(tonie, dict) and str(tonie.get("id", "")) == figure_id:
                    return str(household_id)
        raise RuntimeError(f"Could not find household for figure {figure_id}")

    async def _clear_chapters(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        household_id: str,
        figure_id: str,
    ) -> None:
        response = await client.patch(
            f"{settings.my_tonies_base_url.rstrip('/')}/households/{household_id}/creativetonies/{figure_id}",
            headers={**headers, "Content-Type": "application/json"},
            json={"chapters": []},
        )
        response.raise_for_status()

    async def _upload_file_via_presigned_form(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        audio_file: Path,
    ) -> str:
        request_response = await client.post(
            f"{settings.my_tonies_base_url.rstrip('/')}/file",
            headers={**headers, "Content-Type": "application/json"},
            json={"headers": {}},
        )
        request_response.raise_for_status()
        payload = request_response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Upload init failed: invalid response payload")

        request = payload.get("request")
        file_id = payload.get("fileId")
        if not isinstance(request, dict) or not file_id:
            raise RuntimeError("Upload init failed: missing request or fileId")

        upload_url = request.get("url")
        fields = request.get("fields")
        if not isinstance(upload_url, str) or not isinstance(fields, dict):
            raise RuntimeError("Upload init failed: missing signed upload form data")

        content_type = mimetypes.guess_type(audio_file.name)[0] or "application/octet-stream"
        upload_filename = str(fields.get("key") or audio_file.name)

        with audio_file.open("rb") as fp:
            s3_upload_response = await client.post(
                upload_url,
                data={key: str(value) for key, value in fields.items()},
                files={"file": (upload_filename, fp, content_type)},
            )

        if s3_upload_response.status_code not in {200, 201, 204}:
            raise RuntimeError(
                "S3 upload failed "
                f"(status={s3_upload_response.status_code} body={s3_upload_response.text[:300]})"
            )

        return str(file_id)

    async def _apply_chapters(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        household_id: str,
        figure_id: str,
        chapter_items: list[dict[str, str]],
    ) -> None:
        endpoint = f"{settings.my_tonies_base_url.rstrip('/')}/households/{household_id}/creativetonies/{figure_id}"
        uploaded_file_ids = [item.get("file", "") for item in chapter_items if item.get("file")]
        payload_variants = [
            {"chapters": chapter_items},
            {"chapters": [{"file": item.get("file"), "name": item.get("title")} for item in chapter_items]},
            {"chapters": [{"file": file_id} for file_id in uploaded_file_ids]},
            {"chapters": uploaded_file_ids},
        ]

        last_error: str | None = None
        for payload in payload_variants:
            response = await client.patch(
                endpoint,
                headers={**headers, "Content-Type": "application/json"},
                json=payload,
            )
            if response.is_success:
                return
            last_error = f"status={response.status_code} body={response.text[:500]}"

        raise RuntimeError(f"Applying chapters failed for figure {figure_id}: {last_error}")

    async def _build_auth_headers(self) -> dict[str, str]:
        token = await self._get_access_token()
        return {"Authorization": f"Bearer {token}"}

    async def _get_access_token(self) -> str:
        if settings.my_tonies_api_token:
            return settings.my_tonies_api_token

        if not settings.my_tonies_username or not settings.my_tonies_password:
            raise RuntimeError(
                "Set MY_TONIES_API_TOKEN or MY_TONIES_USERNAME/MY_TONIES_PASSWORD for my.tonies authentication"
            )

        now = time.time()
        if self._cached_token and now < self._cached_token_expires_at:
            return self._cached_token

        async with self._token_lock:
            now = time.time()
            if self._cached_token and now < self._cached_token_expires_at:
                return self._cached_token

            token_data = await self._authenticate_with_oidc()
            access_token = str(token_data.get("access_token", "")).strip()
            if not access_token:
                raise RuntimeError("OIDC authentication succeeded but access_token is missing")

            expires_in = int(token_data.get("expires_in", 300))
            self._cached_token = access_token
            self._cached_token_expires_at = time.time() + max(expires_in - 30, 60)
            return access_token

    async def _authenticate_with_oidc(self) -> dict:
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = self._build_code_challenge(code_verifier)
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)

        auth_query = {
            "client_id": settings.my_tonies_client_id,
            "redirect_uri": settings.my_tonies_redirect_uri,
            "state": state,
            "response_mode": "fragment",
            "response_type": "code",
            "scope": settings.my_tonies_scope,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        if settings.my_tonies_ui_locales:
            auth_query["ui_locales"] = settings.my_tonies_ui_locales

        authorize_url = (
            f"{settings.my_tonies_auth_base_url.rstrip('/')}/auth/realms/tonies/protocol/openid-connect/auth"
            f"?{urlencode(auth_query)}"
        )

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            auth_page = await client.get(authorize_url)
            auth_page.raise_for_status()

            action_url = self._extract_login_action_url(auth_page.text, str(auth_page.url))
            form_data = self._extract_hidden_form_fields(auth_page.text)
            form_data["username"] = settings.my_tonies_username
            form_data["password"] = settings.my_tonies_password

            login_response = await client.post(action_url, data=form_data, follow_redirects=False)
            if login_response.status_code not in {302, 303}:
                raise RuntimeError("Login failed: unexpected response from auth provider")

            location = login_response.headers.get("location", "")
            if not location:
                raise RuntimeError("Login failed: missing redirect location")

            code = self._extract_auth_code_from_location(location)
            if not code:
                raise RuntimeError("Login failed: missing authorization code in redirect")

            token_endpoint = (
                f"{settings.my_tonies_auth_base_url.rstrip('/')}/auth/realms/tonies/protocol/openid-connect/token"
            )
            token_response = await client.post(
                token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "client_id": settings.my_tonies_client_id,
                    "redirect_uri": settings.my_tonies_redirect_uri,
                    "code": code,
                    "code_verifier": code_verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            token_response.raise_for_status()
            payload = token_response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("Token endpoint returned invalid payload")
            return payload

    @staticmethod
    def _build_code_challenge(code_verifier: str) -> str:
        digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")

    @staticmethod
    def _extract_login_action_url(html: str, current_url: str) -> str:
        form_match = re.search(r"<form[^>]*action=['\"]([^'\"]+)['\"]", html, flags=re.IGNORECASE)
        if not form_match:
            raise RuntimeError("Login page did not provide form action")
        action = form_match.group(1)
        return urljoin(current_url, action)

    @staticmethod
    def _extract_hidden_form_fields(html: str) -> dict[str, str]:
        fields: dict[str, str] = {}
        for input_tag in re.findall(r"<input[^>]*>", html, flags=re.IGNORECASE):
            if not re.search(r"type=['\"]hidden['\"]", input_tag, flags=re.IGNORECASE):
                continue
            name_match = re.search(r"name=['\"]([^'\"]+)['\"]", input_tag, flags=re.IGNORECASE)
            if not name_match:
                continue
            value_match = re.search(r"value=['\"]([^'\"]*)['\"]", input_tag, flags=re.IGNORECASE)
            fields[name_match.group(1)] = value_match.group(1) if value_match else ""
        return fields

    @staticmethod
    def _extract_auth_code_from_location(location: str) -> str:
        parsed = urlparse(location)
        fragment_query = parse_qs(parsed.fragment)
        code_values = fragment_query.get("code")
        return code_values[0] if code_values else ""

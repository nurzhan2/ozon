# -*- coding: utf-8 -*-
"""
Выгрузка готовых отчётов в Google Таблицы.

ПОДХОД
    Отчёт сначала собирается в .xlsx (со всем оформлением: жёлтые шапки,
    цветовые шкалы, формулы), а затем заливается в Google Таблицу через
    Drive API с конвертацией. Google сохраняет условное форматирование и
    формулы, поэтому таблица выглядит так же, как файл.

ПОСТОЯННЫЕ ССЫЛКИ
    Если в config.py указан ID таблицы, содержимое ОБНОВЛЯЕТСЯ в ней же —
    ссылка не меняется. Заказчик один раз добавляет 5 ссылок в закладки и
    каждое утро видит свежие данные.
    Если ID не указан, таблица создаётся, и скрипт печатает её ID —
    впишите его в config.py, чтобы дальше обновлялась одна и та же.

ДОСТУП
    Используется сервисный аккаунт Google (JSON-ключ). Инструкция по
    созданию — в файле НАСТРОЙКА.md.
"""

import os
import logging

log = logging.getLogger("ozon.gsheets")

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
GSHEET_MIME = "application/vnd.google-apps.spreadsheet"
SCOPES = ["https://www.googleapis.com/auth/drive"]


class GSheetsError(Exception):
    pass


class GSheetsUploader:
    def __init__(self, credentials_file):
        if not os.path.exists(credentials_file):
            raise GSheetsError(
                f"Не найден файл ключа сервисного аккаунта: {credentials_file}. "
                f"См. НАСТРОЙКА.md, раздел «Google Таблицы»."
            )
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except ImportError:
            raise GSheetsError(
                "Не установлены библиотеки Google. Выполните:\n"
                "  pip install google-api-python-client google-auth"
            )
        creds = service_account.Credentials.from_service_account_file(
            credentials_file, scopes=SCOPES)
        self.email = getattr(creds, "service_account_email", "?")
        self.drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    # ---------------------------------------------------------------
    def upload(self, xlsx_path, spreadsheet_id=None, title=None, share_with=None):
        """
        Заливает xlsx в Google Таблицу.
          spreadsheet_id — обновить существующую (ссылка сохранится);
                           None — создать новую.
        Возвращает (spreadsheet_id, url).
        """
        from googleapiclient.http import MediaFileUpload
        from googleapiclient.errors import HttpError

        media = MediaFileUpload(xlsx_path, mimetype=XLSX_MIME, resumable=False)
        title = title or os.path.splitext(os.path.basename(xlsx_path))[0]

        try:
            if spreadsheet_id:
                self.drive.files().update(
                    fileId=spreadsheet_id,
                    media_body=media,
                    supportsAllDrives=True,
                ).execute()
                file_id = spreadsheet_id
                log.info("обновлена таблица %s (%s)", title, file_id)
            else:
                created = self.drive.files().create(
                    body={"name": title, "mimeType": GSHEET_MIME},
                    media_body=media,
                    fields="id",
                    supportsAllDrives=True,
                ).execute()
                file_id = created["id"]
                log.info("создана таблица %s (%s)", title, file_id)
                if share_with:
                    self.share(file_id, share_with)
        except HttpError as e:
            raise GSheetsError(self._explain(e, spreadsheet_id))

        return file_id, f"https://docs.google.com/spreadsheets/d/{file_id}"

    # ---------------------------------------------------------------
    def share(self, file_id, emails, role="writer"):
        """Выдаёт доступ на таблицу перечисленным адресам."""
        from googleapiclient.errors import HttpError
        for email in emails:
            try:
                self.drive.permissions().create(
                    fileId=file_id,
                    body={"type": "user", "role": role, "emailAddress": email},
                    sendNotificationEmail=False,
                    supportsAllDrives=True,
                ).execute()
                log.info("доступ выдан: %s", email)
            except HttpError as e:
                log.warning("не удалось выдать доступ %s: %s", email, e)

    def check(self, spreadsheet_id):
        """Проверяет, что таблица доступна сервисному аккаунту на запись."""
        from googleapiclient.errors import HttpError
        try:
            info = self.drive.files().get(
                fileId=spreadsheet_id,
                fields="id,name,mimeType,capabilities/canEdit",
                supportsAllDrives=True,
            ).execute()
        except HttpError as e:
            raise GSheetsError(self._explain(e, spreadsheet_id))
        if info.get("mimeType") != GSHEET_MIME:
            raise GSheetsError(
                f"Объект {spreadsheet_id} — не Google Таблица (тип {info.get('mimeType')})."
            )
        if not (info.get("capabilities") or {}).get("canEdit"):
            raise GSheetsError(
                f"Нет прав на запись в «{info.get('name')}». Откройте таблицу и дайте "
                f"сервисному аккаунту {self.email} права Редактора."
            )
        return info.get("name")

    # ---------------------------------------------------------------
    def _explain(self, error, spreadsheet_id):
        """Понятное объяснение типовых ошибок Google API."""
        status = getattr(getattr(error, "resp", None), "status", None)
        text = str(error)
        if status == 404:
            return (f"Таблица {spreadsheet_id} не найдена или недоступна.\n"
                    f"Откройте её и дайте доступ Редактора аккаунту: {self.email}")
        if status == 403:
            if "storageQuotaExceeded" in text:
                return ("У сервисного аккаунта нет своего места на Диске. "
                        "Создайте таблицы вручную в своём Google-аккаунте, дайте им "
                        f"доступ Редактора для {self.email} и впишите их ID в config.py "
                        "(GOOGLE_SHEETS). Тогда скрипт будет только обновлять их.")
            if "insufficientFilePermissions" in text or "forbidden" in text.lower():
                return (f"Недостаточно прав на таблицу {spreadsheet_id}. "
                        f"Дайте аккаунту {self.email} права Редактора (не Читателя).")
            if "accessNotConfigured" in text or "has not been used" in text:
                return ("В проекте Google Cloud не включён Google Drive API. "
                        "Включите его: APIs & Services -> Library -> Google Drive API.")
        return f"Ошибка Google API: {text[:400]}"

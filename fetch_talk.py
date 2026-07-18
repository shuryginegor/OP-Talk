import io
import os
import re
import json
import asyncio
import logging
import httpx
from dotenv import load_dotenv
from urllib.parse import urlparse

# Настройка логирования: выводим время, уровень важности и сообщение
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)


def get_apik_key() -> str:
    load_dotenv()
    local_api_key = os.getenv("TALK_API_KEY")
    return local_api_key


def extract_record_id(url: str) -> str:
    """Извлекает уникальный ID записи (recordingKey) из ссылки."""
    match = re.search(r"/(?:recordings|records)/([a-zA-Z0-9\-]+)", url)
    if match:
        return match.group(1)
    raise ValueError("Не удалось извлечь ID записи из указанной ссылки.")


def get_base_url_from_url(recording_url: str) -> str:
    """Динамически собирает API URL из домена вашей организации."""
    parsed_url = urlparse(recording_url)
    domain = f"{parsed_url.scheme}://{parsed_url.netloc}"
    return f"{domain}/api"


async def download_talk_video(client: httpx.AsyncClient, base_url: str, recording_key: str, api_key: str,
                              quality: str = "900p") -> io.BytesIO | None:
    """Скачивает видеозапись асинхронно и возвращает её в виде потока io.BytesIO."""
    headers = {"X-Auth-Token": api_key, "Accept": "application/octet-stream"}
    endpoint = f"{base_url}/Recordings/{recording_key}/file/{quality}"
    params = {"qualityName": quality}
    logger.info(f"Загрузка видеофайла ({quality})...")

    try:
        async with client.stream("GET", endpoint, headers=headers, params=params) as response:
            if response.status_code != 200:
                error_text = await response.aread()
                logger.warning(
                    f"Не удалось скачать видео (Код {response.status_code}): {error_text.decode('utf-8', errors='ignore')}"
                )
                return None

            video_stream = io.BytesIO()
            async for chunk in response.aiter_bytes(chunk_size=8192):
                if chunk:
                    video_stream.write(chunk)

            video_stream.seek(0)
            logger.info("Видео успешно загружено в буфер памяти.")
            return video_stream
    except Exception as e:
        logger.error(f"Ошибка при скачивании видео: {e}", exc_info=True)
        return None


async def get_text_artifacts(client: httpx.AsyncClient, base_url: str, recording_key: str, api_key: str) -> dict:
    """Запрашивает протокол, пересказ и транскрипцию в формате JSON асинхронно."""
    headers = {"X-Auth-Token": api_key, "Accept": "application/json"}
    endpoint = f"{base_url}/recordings/v2/{recording_key}/summary"
    logger.info("Запрос ИИ-артефактов (транскрипт и саммари)...")

    response = await client.get(endpoint, headers=headers)
    if response.status_code != 200:
        raise Exception(f"Ошибка получения артефактов ({response.status_code}): {response.text}")
    return response.json()


def process_artifacts_to_streams(artifacts_data: dict, recording_key: str) -> dict:
    """Парсит JSON-ответ и возвращает словарик с потоками io.BytesIO."""
    result_streams = {"transcript": None, "summary": None}

    # 1. Формируем Текстовую Расшифровку (Транскрипт)
    transcription_data = artifacts_data.get("transcriptionV2") or {}
    tracks = transcription_data.get("tracks", []) if isinstance(transcription_data, dict) else []
    all_extracted_chunks = []

    for track in tracks:
        speaker_info = track.get("speaker", {}).get("userInfo") or {}
        firstname = speaker_info.get("firstname") or ""
        surname = speaker_info.get("surname") or ""
        speaker_name = f"{firstname} {surname}".strip()
        if not speaker_name:
            speaker_name = track.get("speaker", {}).get("anonymousName") or "Неизвестный спикер"

        chunks = track.get("chunks", [])
        for chunk in chunks:
            all_extracted_chunks.append({
                "time_ms": chunk.get("startTimeOffsetInMillis", 0),
                "speaker": speaker_name,
                "text": chunk.get("text", "")
            })

    if all_extracted_chunks:
        all_extracted_chunks.sort(key=lambda x: x["time_ms"])
        transcript_stream = io.BytesIO()
        with io.TextIOWrapper(transcript_stream, encoding="utf-8", write_through=True) as wrapper:
            for item in all_extracted_chunks:
                offset_ms = item["time_ms"]
                seconds = (offset_ms // 1000) % 60
                minutes = (offset_ms // (1000 * 60)) % 60
                time_str = f"[{minutes:02d}:{seconds:02d}]"
                wrapper.write(f"{time_str} {item['speaker']}: {item['text']}\n")
        transcript_stream.seek(0)
        result_streams["transcript"] = transcript_stream
        logger.info("Транскрипция успешно сформирована в памяти.")
    else:
        logger.warning("Массив реплик пуст. Не удалось обнаружить текст разговора внутри tracks.")

    # 2. Формируем ИИ-пересказ (Саммари)
    summary_data = artifacts_data.get("shortSummaryV2") or {}
    summary_chunks = summary_data.get("chunks", []) if isinstance(summary_data, dict) else []

    if summary_chunks:
        summary_stream = io.BytesIO()
        with io.TextIOWrapper(summary_stream, encoding="utf-8", write_through=True) as wrapper:
            wrapper.write(f"# Краткий пересказ встречи {recording_key}\n\n")
            for chunk in summary_chunks:
                text = chunk.get("text", "")
                if text:
                    wrapper.write(f"{text}\n\n")
        summary_stream.seek(0)
        result_streams["summary"] = summary_stream
        logger.info("ИИ-Пересказ сформирован в памяти.")
    else:
        logger.info("Краткий пересказ (summary) для этой записи отсутствует на сервере.")

    return result_streams


async def download_all_artifacts_by_url(client: httpx.AsyncClient, record_url: str, api_key: str) -> dict:
    """Главная асинхронная функция-воркер."""
    try:
        if not api_key:
            raise ValueError("Для работы скрипта необходим api_key")
        base_url = get_base_url_from_url(record_url)
        recording_key = extract_record_id(record_url)

        video_task = asyncio.create_task(download_talk_video(client, base_url, recording_key, api_key, quality="900p"))
        artifacts_task = asyncio.create_task(get_text_artifacts(client, base_url, recording_key, api_key))

        video_stream, artifacts_json = await asyncio.gather(video_task, artifacts_task)
        text_streams = process_artifacts_to_streams(artifacts_json, recording_key)

        logger.info("Все доступные материалы успешно загружены в оперативную память!")
        return {
            "video": video_stream,
            "transcript": text_streams["transcript"],
            "summary": text_streams["summary"]
        }
    except Exception as e:
        logger.error(f"Ошибка выполнения: {e}", exc_info=True)
        return {"video": None, "transcript": None, "summary": None}


async def is_meet_available(client: httpx.AsyncClient, rescord_url: str, api_key: str) -> dict:
    """Асинхронно проверяет доступность лекции."""
    base_url = get_base_url_from_url(rescord_url)
    recording_key = extract_record_id(rescord_url)

    headers = {"X-Auth-Token": api_key, "Accept": "application/json"}
    endpoint = f"{base_url}/Domain/recordings/{recording_key}"

    response = await client.get(endpoint, headers=headers)
    if response.status_code != 200:
        logger.warning(f"Нет такой лекции (Код {response.status_code}): {response.text}")
        return {}

    data = response.json()
    return {
        "title": data["title"],
        "login": data["createdBy"]["login"],
        "name": data["createdBy"]["firstname"],
        "surname": data["createdBy"]["surname"],
        "date": data["createdDate"]
    }


async def main():
    link = "https://ktalk.ru"
    api_key = get_apik_key()

    async with httpx.AsyncClient() as client:
        meet_info = await is_meet_available(client, link, api_key)
        logger.info(f"Информация о встрече: {meet_info}")

        if meet_info:
            streams = await download_all_artifacts_by_url(client, link, api_key)


if __name__ == "__main__":
    asyncio.run(main())

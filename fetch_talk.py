import io
import os
import re
import json
import asyncio
import logging
import httpx
from dotenv import load_dotenv
from urllib.parse import urlparse
from moviepy import VideoFileClip

# Настройка логирования: выводим время, уровень важности и сообщение
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)


def get_api_key() -> str:
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


async def save_audio(video_path: str, audio_path: str):
    """
    Асинхронно извлекает аудиодорожку MP3 из файла MP4 с помощью moviepy.
    """
    def sync_extraction():
        # Открываем видеофайл
        with VideoFileClip(video_path) as video:
            # logger=None отключает прогресс-бар в консоли, который ломает асинхронный вывод
            video.audio.write_audiofile(
                audio_path,
                codec='mp3',
                logger=None
            )

    # Запускаем синхронный тяжелый процесс в отдельном потоке
    await asyncio.to_thread(sync_extraction)


async def download_talk_video(client: httpx.AsyncClient, base_url: str, recording_key: str, api_key: str,
                              output_path: str, quality: str = "900p") -> bool:
    """Скачивает видеозапись асинхронно и сохраняет её напрямую на диск."""
    headers = {"X-Auth-Token": api_key, "Accept": "application/octet-stream"}
    endpoint = f"{base_url}/Recordings/{recording_key}/file/{quality}"
    params = {"qualityName": quality}
    logger.info(f"Загрузка видеофайла ({quality}) в файл {output_path}...")

    try:
        async with client.stream("GET", endpoint, headers=headers, params=params) as response:
            if response.status_code != 200:
                error_text = await response.aread()
                logger.warning(
                    f"Не удалось скачать видео (Код {response.status_code}): {error_text.decode('utf-8', errors='ignore')}"
                )
                return False

            with open(output_path, "wb") as f:
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            logger.info(f"Видео успешно сохранено на диск: {output_path}")
            return True
    except Exception as e:
        logger.error(f"Ошибка при скачивании видео: {e}", exc_info=True)
        return False


async def get_text_artifacts(client: httpx.AsyncClient, base_url: str, recording_key: str, api_key: str) -> dict | None:
    """Запрашивает протокол, пересказ и транскрипцию в формате JSON асинхронно."""
    headers = {"X-Auth-Token": api_key, "Accept": "application/json"}
    endpoint = f"{base_url}/recordings/v2/{recording_key}/summary"
    logger.info("Запрос ИИ-артефактов (транскрипт и саммари)...")

    try:
        response = await client.get(endpoint, headers=headers, timeout=30.0)
        if response.status_code != 200:
            logger.error(f"Ошибка получения артефактов ({response.status_code}): {response.text}")
            return None
        return response.json()
    except Exception as e:
        logger.error(f"Ошибка при запросе ИИ-артефактов: {e}", exc_info=True)
        return None


def process_and_save_text_artifacts(artifacts_data: dict, recording_key: str, folder_path: str) -> dict:
    """Парсит JSON-ответ, сохраняет файлы на диск и возвращает абсолютные пути к ним."""
    paths = {"transcript": None, "summary": None}

    # 1. Формируем и сохраняем Текстовую Расшифровку (Транскрипт)
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
        transcript_path = os.path.abspath(os.path.join(folder_path, f"{recording_key}_transcript.txt"))

        with open(transcript_path, "w", encoding="utf-8") as f:
            for item in all_extracted_chunks:
                offset_ms = item["time_ms"]
                seconds = (offset_ms // 1000) % 60
                minutes = (offset_ms // (1000 * 60)) % 60
                time_str = f"[{minutes:02d}:{seconds:02d}]"
                f.write(f"{time_str} {item['speaker']}: {item['text']}\n")

        logger.info(f"Транскрипция успешно записана на диск: {transcript_path}")
        paths["transcript"] = transcript_path
    else:
        logger.warning("Массив реплик пуст. Текст разговора отсутствует.")

    # 2. Формируем и сохраняем ИИ-пересказ (Саммари)
    summary_data = artifacts_data.get("shortSummaryV2") or {}
    summary_chunks = summary_data.get("chunks", []) if isinstance(summary_data, dict) else []

    if summary_chunks:
        summary_path = os.path.abspath(os.path.join(folder_path, f"{recording_key}_summary.txt"))

        with open(summary_path, "w", encoding="utf-8") as f:
            f.write(f"Краткий пересказ встречи {recording_key}\n\n")
            for chunk in summary_chunks:
                text = chunk.get("text", "")
                if text:
                    f.write(f"{text}\n\n")

        logger.info(f"ИИ-Пересказ успешно записан на диск: {summary_path}")
        paths["summary"] = summary_path
    else:
        logger.info("Краткий пересказ (summary) для этой записи отсутствует на сервере.")

    # protocol_data = artifacts_data.get("protocolV2") or {}
    # protocol_chunks = protocol_data.get("chunks", []) if isinstance(protocol_data, dict) else []
    #
    # if protocol_chunks:
    #     protocol_path = os.path.abspath(os.path.join(folder_path, f"{recording_key}_protocol.txt"))
    #
    #     with open(protocol_path, "w", encoding="utf-8") as f:
    #         f.write(f"Протокол встречи {recording_key}\n\n")
    #         for chunk in protocol_chunks:
    #             text = chunk.get("text", "")
    #             if text:
    #                 f.write(f"{text}\n\n")
    #
    #     logger.info(f"ИИ-Протокол успешно записан на диск: {protocol_path}")
    #     paths["protocol"] = protocol_path
    # else:
    #     logger.info("Протокол (protocol) для этой записи отсутствует на сервере.")
    return paths


async def download_all_artifacts_by_url(record_url: str, api_key: str) -> dict:
    """Главная асинхронная функция-воркер. Возвращает словарь с абсолютными путями к сохраненным файлам."""
    try:
        if not api_key:
            raise ValueError("Для работы скрипта необходим api_key")

        base_url = get_base_url_from_url(record_url)
        recording_key = extract_record_id(record_url)

        # Создаем целевую директорию artifacts
        artifacts_dir = "artifacts"
        os.makedirs(artifacts_dir, exist_ok=True)

        video_output_path = os.path.join(artifacts_dir, f"{recording_key}_video.mp4")
        audio_output_path = os.path.join(artifacts_dir, f"{recording_key}_audio.mp3")

        # Настройки таймаутов, убирающие ошибку ReadTimeout при скачивании тяжелых видео чанками
        timeout_settings = httpx.Timeout(connect=15.0, read=None, write=20.0, pool=15.0)

        video_res = None
        artifacts_res = None

        async with httpx.AsyncClient(timeout=timeout_settings) as client:
            video_task = asyncio.create_task(
                download_talk_video(client, base_url, recording_key, api_key, video_output_path, quality="900p")
            )
            # В gather оставляем только независимые задачи: скачивание видео и получение текста
            artifacts_task = asyncio.create_task(
                get_text_artifacts(client, base_url, recording_key, api_key)
            )

            results = await asyncio.gather(video_task, artifacts_task, return_exceptions=True)

            video_res = results[0]
            artifacts_res = results[1]

        # Анализируем результаты задачи скачивания видео
        video_has_error = isinstance(video_res, Exception)
        if video_has_error:
            logger.error(f"Задача скачивания видео завершилась исключением: {video_res}")

        # Проверяем, что видео успешно скачалось и физически существует
        video_result_path = None
        audio_result_path = None

        if not video_has_error and os.path.exists(video_output_path):
            video_result_path = os.path.abspath(video_output_path)

            # ТЕПЕРЬ запуск извлечения аудио безопасен, так как видео уже на диске
            try:
                logger.info("Начало извлечения аудиодорожки...")
                await save_audio(video_output_path, audio_output_path)
                if os.path.exists(audio_output_path):
                    audio_result_path = os.path.abspath(audio_output_path)
            except Exception as audio_err:
                logger.error(f"Не удалось извлечь аудиодорожку: {audio_err}", exc_info=True)
        else:
            logger.warning("Скачивание видео не завершилось успехом, извлечение аудио пропущено.")

        # Анализируем результаты задачи получения текста
        artifacts_has_error = isinstance(artifacts_res, Exception)
        if artifacts_has_error:
            logger.error(f"Задача получения текста завершилась исключением: {artifacts_res}")

        text_paths = {"transcript": None, "summary": None}
        if not artifacts_has_error and artifacts_res:
            text_paths = process_and_save_text_artifacts(artifacts_res, recording_key, artifacts_dir)

        logger.info("Обработка всех доступных материалов завершена!")

        # Возвращаем полный набор путей, включая аудио
        return {
            "video": video_result_path,
            "audio": audio_result_path,
            "transcript": text_paths["transcript"],
            "summary": text_paths["summary"]
        }

    except Exception as e:
        logger.error(f"Ошибка выполнения download_all_artifacts_by_url: {e}", exc_info=True)
        return {"video": None, "audio": None, "transcript": None, "summary": None}


async def is_meet_available(record_url: str, api_key: str) -> dict:
    """Асинхронно проверяет доступность лекции с защитой от сетевых ошибок и таймаутов."""
    base_url = get_base_url_from_url(record_url)
    recording_key = extract_record_id(record_url)

    headers = {"X-Auth-Token": api_key, "Accept": "application/json"}
    endpoint = f"{base_url}/Domain/recordings/{recording_key}"

    # Настраиваем таймауты (5 секунд на коннект, 10 на чтение данных)
    timeout_settings = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

    # Делаем несколько попыток на случай кратковременного сбоя сети
    max_retries = 5

    for attempt in range(1, max_retries + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout_settings) as client:
                response = await client.get(endpoint, headers=headers)

            # Если запрос прошел, выходим из цикла попыток
            break
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as net_err:
            logger.warning(f"Попытка {attempt}/{max_retries} провалилась. Сетевая ошибка: {net_err}")
            if attempt == max_retries:
                logger.error("Все попытки связаться с сервером КТалк исчерпаны.")
                return {}  # Возвращаем пустой словарь, бот не упадет
            await asyncio.sleep(1)  # Небольшая пауза перед повторным запросом
        except Exception as e:
            logger.error(f"Непредвиденная ошибка при запросе к КТалк: {e}", exc_info=True)
            return {}

    # Проверяем HTTP-статус ответа сервера
    if response.status_code != 200:
        logger.warning(f"Нет такой лекции (Код {response.status_code}): {response.text}")
        return {}

    try:
        data = response.json()

        # Безопасное извлечение через .get(), чтобы не упасть по KeyError, если структура JSON изменится
        created_by = data.get("createdBy") or {}

        return {
            "title": data.get("title", "Без названия"),
            "login": created_by.get("login", ""),
            "name": created_by.get("firstname", ""),
            "surname": created_by.get("surname", ""),
            "date": data.get("createdDate", "")
        }
    except Exception as parse_err:
        logger.error(f"Ошибка при парсинге JSON ответа КТалк: {parse_err}", exc_info=True)
        return {}


async def delete_files(artifacts):
    for artifact in artifacts:
        if artifacts[artifact] is not None:
            file_path = artifacts[artifact]

            def sync_delete():
                if os.path.exists(file_path):
                    os.remove(file_path)
                    return True
                return False

            try:
                # Выполняем проверку и удаление в фоновом потоке
                file_deleted = await asyncio.to_thread(sync_delete)
                if file_deleted:
                    logger.info(f"Файл успешно удален: {file_path}")
                    return True
                else:
                    logger.warning(f"Файл не найден для удаления: {file_path}")
            except Exception as e:
                logger.error(f"Error in deleting file {file_path}: {e}")


async def main():
    link = "https://ktalk.ru"
    api_key = get_api_key()

    async with httpx.AsyncClient() as client:
        meet_info = await is_meet_available(link, api_key)
        logger.info(f"Информация о встрече: {meet_info}")

        if meet_info:
            streams = await download_all_artifacts_by_url(link, api_key)


if __name__ == "__main__":
    asyncio.run(main())

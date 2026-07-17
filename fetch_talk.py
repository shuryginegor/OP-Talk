import io
import os
import re
import json
import requests
from dotenv import load_dotenv
from urllib.parse import urlparse


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


def download_talk_video(base_url: str, recording_key: str, api_key: str, quality: str = "900p") -> io.BytesIO | None:
    """Скачивает видеозапись и возвращает её в виде потока io.BytesIO."""
    headers = {"X-Auth-Token": api_key, "Accept": "application/octet-stream"}
    endpoint = f"{base_url}/Recordings/{recording_key}/file/{quality}"
    params = {"qualityName": quality}
    print(f"Загрузка видеофайла ({quality})...")

    with requests.get(endpoint, headers=headers, params=params, stream=True) as response:
        if response.status_code != 200:
            print(f"⚠️ Не удалось скачать видео (Код {response.status_code}): {response.text}")
            return None

        video_stream = io.BytesIO()
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                video_stream.write(chunk)

        video_stream.seek(0)  # Сбрасываем указатель в начало потока для последующего чтения
        print("✔️ Видео успешно загружено в буфер памяти.")
        return video_stream


def get_text_artifacts(base_url: str, recording_key: str, api_key: str) -> dict:
    """Запрашивает протокол, пересказ и транскрипцию в формате JSON."""
    headers = {"X-Auth-Token": api_key, "Accept": "application/json"}
    endpoint = f"{base_url}/recordings/v2/{recording_key}/summary"
    print("Запрос ИИ-артефактов (транскрипт и саммари)...")
    response = requests.get(endpoint, headers=headers)
    if response.status_code != 200:
        raise Exception(f"Ошибка получения артефактов ({response.status_code}): {response.text}")
    return response.json()


def process_artifacts_to_streams(artifacts_data: dict, recording_key: str) -> dict:
    """Парсит JSON-ответ и возвращает словарик с потоками io.BytesIO вместо записи на диск."""
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

        # Нам нужен текстовый буфер поверх байтового для кодирования строк в utf-8
        with io.TextIOWrapper(transcript_stream, encoding="utf-8", write_through=True) as wrapper:
            for item in all_extracted_chunks:
                offset_ms = item["time_ms"]
                seconds = (offset_ms // 1000) % 60
                minutes = (offset_ms // (1000 * 60)) % 60
                time_str = f"[{minutes:02d}:{seconds:02d}]"
                wrapper.write(f"{time_str} {item['speaker']}: {item['text']}\n")

        transcript_stream.seek(0)
        result_streams["transcript"] = transcript_stream
        print("✔️ Транскрипция успешно сформирована в памяти.")
    else:
        print("⚠️ Массив реплик пуст. Не удалось обнаружить текст разговора внутри tracks.")

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
        print("✔️ ИИ-Пересказ сформирован в памяти.")
    else:
        print("💡 Краткий пересказ (summary) для этой записи отсутствует на сервере.")

    return result_streams


def download_all_artifacts_by_url(record_url: str, api_key: str) -> dict:
    """Главная функция-воркер, возвращающая словарь со всеми потоками."""
    try:
        if not api_key:
            raise ValueError("Для работы скрипта необходим api_key")

        base_url = get_base_url_from_url(record_url)
        recording_key = extract_record_id(record_url)

        # Шаг 1. Скачиваем видеофайл в память
        video_stream = download_talk_video(base_url, recording_key, api_key, quality="900p")

        # Шаг 2. Запрашиваем JSON с текстовыми артефактами
        artifacts_json = get_text_artifacts(base_url, recording_key, api_key)

        # Шаг 3. Парсим JSON и генерируем байтовые потоки
        text_streams = process_artifacts_to_streams(artifacts_json, recording_key)

        print("🎉 Все доступные материалы успешно загружены в оперативную память!")

        return {
            "video": video_stream,
            "transcript": text_streams["transcript"],
            "summary": text_streams["summary"]
        }
    except Exception as e:
        print(f"❌ Ошибка выполнения: {e}")
        return {"video": None, "transcript": None, "summary": None}


def is_meet_available(rescord_url: str, api_key: str) -> bool:
    base_url = get_base_url_from_url(rescord_url)
    recording_key = extract_record_id(rescord_url)
    """Скачивает видеозапись и возвращает её в виде потока io.BytesIO."""
    headers = {"X-Auth-Token": api_key, "Accept": "application/octet-stream"}
    endpoint = f"api/Domain/recordings/{recording_key}"

    with requests.get(endpoint, headers=headers, params=params, stream=True) as response:
        if response.status_code != 200:
            print(f"⚠️ Не удалось скачать видео (Код {response.status_code}): {response.text}")
            return None

        video_stream = io.BytesIO()
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                video_stream.write(chunk)

        video_stream.seek(0)  # Сбрасываем указатель в начало потока для последующего чтения
        print("✔️ Видео успешно загружено в буфер памяти.")
        return video_stream


# Пример использования, где файлы из потоков можно, например, отправить в S3, Telegram или сохранить на диск
if __name__ == "__main__":
    load_dotenv()
    local_api_key = os.getenv("TALK_API_KEY")
    link = "https://5d5nbodd.ktalk.ru/recordings/xIu6mXwBVohle3V4anyX"

    # Получаем словарь с BytesIO потоками
    streams = download_all_artifacts_by_url(record_url=link, api_key=local_api_key)


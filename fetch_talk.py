import os
import re
import json
import requests
from dotenv import load_dotenv
from urllib.parse import urlparse


def extract_record_id(url: str) -> str:
    """
    Извлекает уникальный ID записи (recordingKey) из ссылки.
    """
    match = re.search(r"/(?:recordings|records)/([a-zA-Z0-9\-]+)", url)
    if match:
        return match.group(1)
    raise ValueError("Не удалось извлечь ID записи из указанной ссылки.")


def get_base_url_from_url(recording_url: str) -> str:
    """
    Динамически собирает API URL из домена вашей организации.
    """
    parsed_url = urlparse(recording_url)
    domain = f"{parsed_url.scheme}://{parsed_url.netloc}"
    return f"{domain}/api"


def download_talk_video(base_url: str, recording_key: str, api_key: str, output_path: str, quality: str = "900p"):
    """
    Скачивает видеозапись по официальному медиа-эндпоинту Толка.
    GET /api/Recordings/{recordingKey}/file/{qualityName}
    """
    headers = {
        "X-Auth-Token": api_key,
        "Accept": "application/octet-stream"
    }
    endpoint = f"{base_url}/Recordings/{recording_key}/file/{quality}"
    params = {"qualityName": quality}

    print(f"Загрузка видеофайла ({quality})...")
    with requests.get(endpoint, headers=headers, params=params, stream=True) as response:
        if response.status_code != 200:
            print(f"⚠️ Не удалось скачать видео (Код {response.status_code}): {response.text}")
            return False

        with open(output_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
    print(f"✔️ Видео успешно сохранено: {output_path}")
    return True


def get_text_artifacts(base_url: str, recording_key: str, api_key: str) -> dict:
    """
    Запрашивает протокол, пересказ и транскрипцию в формате JSON строго по документации.
    GET /api/recordings/v2/{recordingKey}/summary
    """
    headers = {
        "X-Auth-Token": api_key,
        "Accept": "application/json"
    }
    # Строго по присланному PDF (маленькие буквы, v2)
    endpoint = f"{base_url}/recordings/v2/{recording_key}/summary"

    print("Запрос ИИ-артефактов (транскрипт и саммари)...")
    response = requests.get(endpoint, headers=headers)

    if response.status_code != 200:
        raise Exception(f"Ошибка получения артефактов ({response.status_code}): {response.text}")

    return response.json()


def save_processed_artifacts(artifacts_data: dict, recording_key: str, output_folder: str):
    """
    Парсит JSON-ответ от Толка, извлекая реплики из дорожек (tracks)
    и автоматически сопоставляя их с именами спикеров.
    """
    # 1. Извлекаем и сохраняем Текстовую Расшифровку (Транскрипт)
    transcription_data = artifacts_data.get("transcriptionV2") or {}
    tracks = transcription_data.get("tracks", []) if isinstance(transcription_data, dict) else []

    # Собираем все реплики со всех дорожек в один общий плоский список
    all_extracted_chunks = []

    for track in tracks:
        # Пытаемся получить имя спикера
        speaker_info = track.get("speaker", {}).get("userInfo") or {}
        firstname = speaker_info.get("firstname") or ""
        surname = speaker_info.get("surname") or ""

        # Собираем полное имя, убирая лишние пробелы, если чего-то нет
        speaker_name = f"{firstname} {surname}".strip()
        if not speaker_name:
            speaker_name = track.get("speaker", {}).get("anonymousName") or "Неизвестный спикер"

        chunks = track.get("chunks", [])
        for chunk in chunks:
            # Сохраняем саму реплику, её таймкод и автора
            all_extracted_chunks.append({
                "time_ms": chunk.get("startTimeOffsetInMillis", 0),
                "speaker": speaker_name,
                "text": chunk.get("text", "")
            })

    # Если реплики найдены, сортируем их по времени (чтобы диалог шёл по порядку, если спикеры перебивали друг друга)
    if all_extracted_chunks:
        all_extracted_chunks.sort(key=lambda x: x["time_ms"])

        transcript_path = os.path.join(output_folder, f"transcript_{recording_key}.txt")
        with open(transcript_path, "w", encoding="utf-8") as f:
            for item in all_extracted_chunks:
                offset_ms = item["time_ms"]
                # Переводим миллисекунды в таймкод [ММ:СС]
                seconds = (offset_ms // 1000) % 60
                minutes = (offset_ms // (1000 * 60)) % 60
                time_str = f"[{minutes:02d}:{seconds:02d}]"

                f.write(f"{time_str} {item['speaker']}: {item['text']}\n")

        print(f"✔️ Транскрипция успешно сохранена (с именами спикеров): {transcript_path}")
    else:
        print("⚠️ Массив реплик пуст. Не удалось обнаружить текст разговора внутри tracks.")

    # 2. Извлекаем и сохраняем ИИ-пересказ (Саммари)
    summary_data = artifacts_data.get("shortSummaryV2") or {}
    summary_chunks = summary_data.get("chunks", []) if isinstance(summary_data, dict) else []

    if summary_chunks:
        summary_path = os.path.join(output_folder, f"summary_{recording_key}.md")
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write(f"# Краткий пересказ встречи {recording_key}\n\n")
            for chunk in summary_chunks:
                text = chunk.get("text", "")
                if text:
                    f.write(f"{text}\n\n")
        print(f"✔️ ИИ-Пересказ сохранен: {summary_path}")
    else:
        print("💡 Краткий пересказ (summary) для этой записи отсутствует на сервере.")


def download_all_artifacts_by_url(record_url: str, api_key: str, output_folder: str = "./"):
    """
    Главная функция-воркер архитектуры проекта.
    """
    try:
        if not api_key:
            raise ValueError("Для работы скрипта необходим api_key")

        base_url = get_base_url_from_url(record_url)
        recording_key = extract_record_id(record_url)

        os.makedirs(output_folder, exist_ok=True)

        # Шаг 1. Скачиваем видеофайл встречи
        video_path = os.path.join(output_folder, f"video_{recording_key}.mp4")
        download_talk_video(base_url, recording_key, api_key, video_path, quality="900p")

        # Шаг 2. Запрашиваем JSON с текстовыми артефактами
        artifacts_json = get_text_artifacts(base_url, recording_key, api_key)

        # ВРЕМЕННЫЙ ВЫВОД: Печатаем весь JSON от Контура, чтобы увидеть реальные ключи
        print("\n=== СЫРОЙ ОТВЕТ СЕРВЕРА ===")
        print(json.dumps(artifacts_json, indent=2, ensure_ascii=False))
        print("===========================\n")

        # Шаг 3. Парсим JSON и сохраняем файлы
        save_processed_artifacts(artifacts_json, recording_key, output_folder)

        print("🎉 Все доступные материалы успешно скачаны и разложены по файлам!")

    except Exception as e:
        print(f"❌ Ошибка выполнения: {e}")


# Пример использования, где переменная извлекается только в блоке запуска:
if __name__ == "__main__":
    load_dotenv()
    local_api_key = os.getenv("TALK_API_KEY")

    link = "https://5d5nbodd.ktalk.ru/recordings/xIu6mXwBVohle3V4anyX"

    # Вызываем главную функцию, передавая ключ аргументом
    download_all_artifacts_by_url(record_url=link, api_key=local_api_key)

import os
import sys
import json
import hashlib
import shutil
import subprocess
import logging
import argparse
import tempfile
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from PIL import Image, ImageChops


logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

logger = logging.getLogger("DocumentConverter")

DOC_EXTENSIONS = {".docx", ".doc", ".xls", ".xlsx"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tiff", ".gif"}
SUPPORTED_EXTENSIONS = DOC_EXTENSIONS | IMAGE_EXTENSIONS


def strip_hash_prefix(filename):
    return re.sub(r"^[a-fA-F0-9]{4}_", "", filename)


class DatabaseManager:
    def __init__(self, db_path, list_json_path=None):
        self.db_path = db_path
        self.list_json_path = list_json_path
        self.data = self.load()

    def load(self):
        if not os.path.exists(self.db_path):
            return {}

        try:
            with open(self.db_path, "r", encoding="utf-8") as file:
                data = json.load(file)
                return data if isinstance(data, dict) else {}
        except Exception as error:
            logger.warning("Не удалось прочитать JSON: %s", error)
            return {}

    def save(self):
        directory = os.path.dirname(os.path.abspath(self.db_path))
        os.makedirs(directory, exist_ok=True)

        temporary_path = self.db_path + ".tmp"
        with open(temporary_path, "w", encoding="utf-8") as file:
            json.dump(self.data, file, ensure_ascii=False, indent=4)
            file.write("\n")

        os.replace(temporary_path, self.db_path)

        if self.list_json_path:
            self.save_list()

    def save_list(self):
        if not self.list_json_path:
            return

        directory = os.path.dirname(os.path.abspath(self.list_json_path))
        os.makedirs(directory, exist_ok=True)

        images_list = sorted(list(self.data.keys()))

        temporary_path = self.list_json_path + ".tmp"
        with open(temporary_path, "w", encoding="utf-8") as file:
            json.dump(images_list, file, ensure_ascii=False, indent=4)
            file.write("\n")

        os.replace(temporary_path, self.list_json_path)

    def remove_photos(self, image_names, output_dir):
        for image_name in image_names:
            image_path = os.path.join(output_dir, image_name)

            try:
                if os.path.isfile(image_path):
                    os.remove(image_path)
            except OSError as error:
                logger.warning("Не удалось удалить %s: %s", image_path, error)

            self.data.pop(image_name, None)


def calculate_sha256(file_path):
    sha256 = hashlib.sha256()

    with open(file_path, "rb") as file:
        while True:
            chunk = file.read(1024 * 1024)
            if not chunk:
                break
            sha256.update(chunk)

    return sha256.hexdigest()


def check_dependencies():
    missing = []

    for command in ("libreoffice", "pdftoppm"):
        if shutil.which(command) is None:
            missing.append(command)

    if missing:
        raise RuntimeError(
            "Не найдены программы: " + ", ".join(missing) +
            ". Установите LibreOffice и poppler-utils."
        )


def crop_image_bbox(img, margin=0):
    try:
        img_rgb = img.convert("RGB")
        bg = Image.new("RGB", img_rgb.size, (255, 255, 255))
        diff = ImageChops.difference(img_rgb, bg)
        bbox = diff.getbbox()

        if bbox:
            left = max(0, bbox[0] - margin)
            upper = max(0, bbox[1] - margin)
            right = min(img_rgb.width, bbox[2] + margin)
            lower = min(img_rgb.height, bbox[3] + margin)
            return img.crop((left, upper, right, lower))
    except Exception as e:
        logger.warning("Ошибка при обрезке полей: %s", e)
    return img


class DocumentConverter:
    def __init__(self, config_path):
        self.config = self.load_config(config_path)

        self.source_dir = os.path.abspath(self.config["source_dir"])
        self.output_dir = os.path.abspath(self.config["output_dir"])
        self.db_path = os.path.abspath(self.config["json_path"])

        list_path_config = self.config.get("list_json_path")
        if list_path_config:
            self.list_json_path = os.path.abspath(list_path_config)
        else:
            base, ext = os.path.splitext(self.db_path)
            self.list_json_path = f"{base}_list{ext}"

        self.max_workers = max(1, int(self.config.get("max_workers", 4)))
        self.dpi = int(self.config.get("png_dpi", 150))

        self.db = DatabaseManager(self.db_path, self.list_json_path)
        os.makedirs(self.output_dir, exist_ok=True)

        self.stats = {
            "new": 0,
            "updated": 0,
            "unchanged": 0,
            "errors": 0,
            "deleted": 0
        }

    @staticmethod
    def load_config(config_path):
        if not os.path.isfile(config_path):
            raise FileNotFoundError("Файл конфигурации не найден: " + config_path)

        with open(config_path, "r", encoding="utf-8") as file:
            config = json.load(file)

        required = ("source_dir", "output_dir", "json_path")
        missing = [key for key in required if key not in config]
        if missing:
            raise ValueError("В config.json отсутствуют поля: " + ", ".join(missing))

        return config

    def make_base_image_name(self, source_file):
        relative_path = os.path.relpath(source_file, self.source_dir)
        path_without_extension, ext = os.path.splitext(relative_path)
        ext_lower = ext.lower()

        if ext_lower in IMAGE_EXTENSIONS:
            target_ext = ext_lower
        else:
            target_ext = ".png"

        normalized_name_part = os.path.normpath(path_without_extension)
        transformed_name = normalized_name_part.replace(os.sep, "_")

        original_ext_without_dot = ext_lower[1:] if ext_lower else "noext"
        if original_ext_without_dot:
            transformed_name = f"{transformed_name}_{original_ext_without_dot}"

        return transformed_name + target_ext

    def make_image_name(self, source_file, file_hash):
        base_name = self.make_base_image_name(source_file)
        prefix = file_hash[:4] if file_hash else ""
        return f"{prefix}_{base_name}" if prefix else base_name

    def find_existing_entry(self, base_name):
        for img_name, info in self.db.data.items():
            if strip_hash_prefix(img_name) == base_name:
                return img_name, info
        return None, None

    def convert_to_pdf(self, source_file, temp_dir):
        profile_dir = os.path.join(temp_dir, "libreoffice_profile")
        os.makedirs(profile_dir, exist_ok=True)

        ext = os.path.splitext(source_file)[1].lower()

        if ext in (".xls", ".xlsx"):
            convert_spec = 'pdf:calc_pdf_Export:{"SinglePageSheets":{"type":"boolean","value":"true"}}'
        else:
            convert_spec = 'pdf'

        command = [
            "libreoffice",
            "--headless",
            "--convert-to", convert_spec,
            "--outdir", temp_dir,
            "-env:UserInstallation=file://" + os.path.abspath(profile_dir),
            source_file
        ]

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        if result.returncode != 0 and convert_spec != 'pdf':
            command[3] = 'pdf'
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "неизвестная ошибка"
            raise RuntimeError("Ошибка LibreOffice: " + message)

        pdf_name = os.path.splitext(os.path.basename(source_file))[0] + ".pdf"
        pdf_path = os.path.join(temp_dir, pdf_name)

        if not os.path.isfile(pdf_path):
            message = result.stderr.strip() or result.stdout.strip() or "PDF не создан"
            raise RuntimeError("LibreOffice не создал PDF: " + message)

        return pdf_path

    def convert_pdf_to_png(self, pdf_path, temp_dir, target_image_name):
        final_path = os.path.join(self.output_dir, target_image_name)
        temporary_prefix = os.path.join(temp_dir, "page")

        command = [
            "pdftoppm",
            "-png",
            "-r", str(self.dpi),
            pdf_path,
            temporary_prefix
        ]

        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "неизвестная ошибка"
            raise RuntimeError("Ошибка pdftoppm: " + message)

        page_files = []
        for filename in os.listdir(temp_dir):
            if filename.startswith("page-") and filename.endswith(".png"):
                page_files.append(os.path.join(temp_dir, filename))

        def get_page_number(filepath):
            match = re.search(r"page-(\d+)\.png", os.path.basename(filepath))
            return int(match.group(1)) if match else 0

        page_files.sort(key=get_page_number)

        if not page_files:
            raise RuntimeError("pdftoppm не создал ни одного изображения страниц")

        cropped_pages = []
        for p in page_files:
            with Image.open(p) as img:
                cropped = crop_image_bbox(img, margin=0)
                cropped_pages.append(cropped)

        max_width = max(img.width for img in cropped_pages)
        total_height = sum(img.height for img in cropped_pages)

        combined_image = Image.new("RGB", (max_width, total_height), (255, 255, 255))
        y_offset = 0

        for img in cropped_pages:
            x_offset = (max_width - img.width) // 2
            combined_image.paste(img, (x_offset, y_offset))
            y_offset += img.height
            img.close()

        final_image = crop_image_bbox(combined_image, margin=20)
        final_image.save(final_path)

        return target_image_name

    def process_file(self, source_file, force=False):
        current_hash = calculate_sha256(source_file)
        base_name = self.make_base_image_name(source_file)

        old_image_name, existing_info = self.find_existing_entry(base_name)

        old_hash = existing_info.get("hash", "") if isinstance(existing_info, dict) else ""
        is_new = existing_info is None
        is_changed = is_new or current_hash != old_hash

        if not is_changed and not force:
            return "unchanged", source_file, {}, None

        new_image_name = self.make_image_name(source_file, current_hash)
        old_to_remove = old_image_name if (old_image_name and old_image_name != new_image_name) else None

        ext = os.path.splitext(source_file)[1].lower()

        if ext in IMAGE_EXTENSIONS:
            final_path = os.path.join(self.output_dir, new_image_name)
            shutil.copyfile(source_file, final_path)
            with Image.open(final_path) as img:
                cropped = crop_image_bbox(img, margin=20)
                cropped.save(final_path)

            new_data = {
                new_image_name: {
                    "hash": current_hash
                }
            }

            status = "new" if is_new else "updated"
            return status, source_file, new_data, old_to_remove

        temporary_dir = tempfile.mkdtemp(
            prefix="document_converter_",
            dir=self.output_dir
        )

        try:
            pdf_path = self.convert_to_pdf(source_file, temporary_dir)
            image_name = self.convert_pdf_to_png(
                pdf_path,
                temporary_dir,
                new_image_name
            )

            new_data = {
                image_name: {
                    "hash": current_hash
                }
            }

            status = "new" if is_new else "updated"
            return status, source_file, new_data, old_to_remove

        finally:
            shutil.rmtree(temporary_dir, ignore_errors=True)

    def scan_files(self):
        files = []

        for root, _, names in os.walk(self.source_dir):
            for name in names:
                if name.startswith("~$") or name.startswith("."):
                    continue

                extension = os.path.splitext(name)[1].lower()
                if extension not in SUPPORTED_EXTENSIONS:
                    continue

                files.append(os.path.abspath(os.path.join(root, name)))

        return files

    def remove_deleted_sources(self, current_files):
        valid_base_names = {self.make_base_image_name(f) for f in current_files}
        stale_images = [
            img_name for img_name in self.db.data.keys()
            if strip_hash_prefix(img_name) not in valid_base_names
        ]

        for img_name in stale_images:
            self.db.remove_photos([img_name], self.output_dir)
            self.stats["deleted"] += 1
            logger.info("Удалено изображение отсутствующего файла: %s", img_name)

    def run(self, force=False):
        if not os.path.isdir(self.source_dir):
            raise NotADirectoryError("Исходная директория не найдена: " + self.source_dir)

        files = self.scan_files()

        has_documents = any(os.path.splitext(f)[1].lower() in DOC_EXTENSIONS for f in files)
        if has_documents:
            check_dependencies()

        logger.info("Запуск обхода директории: %s", self.source_dir)

        current_files = set(files)
        self.remove_deleted_sources(current_files)

        logger.info(
            "Найдено файлов: %d. Запуск в %d потоков...",
            len(files),
            self.max_workers
        )

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            tasks = {}

            for source_file in files:
                future = executor.submit(
                    self.process_file,
                    source_file,
                    force
                )
                tasks[future] = source_file

            for future in as_completed(tasks):
                source_file = tasks[future]

                try:
                    status, source_path, new_data, old_to_remove = future.result()

                    if old_to_remove:
                        self.db.remove_photos([old_to_remove], self.output_dir)

                    if status == "unchanged":
                        self.stats["unchanged"] += 1
                        logger.info("Без изменений: %s", source_path)
                        continue

                    self.db.data.update(new_data)
                    self.stats[status] += 1
                    label = "Новый" if status == "new" else "Обновлённый"
                    logger.info("%s файл: %s", label, source_path)

                except Exception as error:
                    self.stats["errors"] += 1
                    logger.error("Ошибка при обработке %s: %s", source_file, error)

        self.db.save()
        logger.info("Основной JSON сохранён: %s", self.db_path)
        logger.info("JSON со списком фото сохранён: %s", self.list_json_path)
        self.print_stats()

    def print_stats(self):
        logger.info("\nСТАТИСТИКА:")
        logger.info(f"   Новые файлы: {self.stats['new']}")
        logger.info(f"   Обновлённые файлы: {self.stats['updated']}")
        logger.info(f"   Без изменений: {self.stats['unchanged']}")
        logger.info(f"   Ошибки: {self.stats['errors']}")
        logger.info(f"   Удалённые файлы: {self.stats['deleted']}")


def main():
    parser = argparse.ArgumentParser(
        description="Конвертация документов в PNG и обработка изображений"
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Путь к config.json"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Переконвертировать все документы"
    )

    args = parser.parse_args()

    try:
        converter = DocumentConverter(args.config)
        converter.run(force=args.force)

    except Exception as error:
        logger.error("Ошибка: %s", error)
        sys.exit(1)


if __name__ == "__main__":
    main()

import os, sys, json, hashlib, shutil, subprocess, logging, argparse, tempfile, re
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image, ImageChops

logging.basicConfig(level=logging.INFO, format="%(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("Converter")

DOCS = {".docx", ".doc", ".xls", ".xlsx"}
IMGS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tiff", ".gif"}
ALL = DOCS | IMGS


class DB:
    """Простая JSON база."""

    def __init__(self, path, list_path=None):
        """Запоминает пути и грузит данные."""
        self.path = path
        self.list_path = list_path
        self.data = self.load()

    def load(self):
        """Читает JSON или даёт пустой словарь."""
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception as e:
            log.warning("JSON не прочитан: %s", e)
            return {}

    def save(self):
        """Пишет базу в файл."""
        self._write(self.path, self.data)
        if self.list_path:
            self._write(self.list_path, sorted(self.data))

    def _write(self, path, data):
        """Пишет данные во временный файл и заменяет."""
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
            f.write("\n")
        os.replace(tmp, path)

    def remove(self, names, out_dir):
        """Удаляет картинки и записи."""
        for n in names:
            p = os.path.join(out_dir, n)
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except OSError as e:
                log.warning("Не удалить %s: %s", p, e)
            self.data.pop(n, None)


class Files:
    """Хэш, обрезка, проверка программ."""

    @staticmethod
    def sha256(path):
        """Считает хэш файла."""
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def strip_hash(name):
        """Убирает 4 символа хэша в начале."""
        return re.sub(r"^[a-fA-F0-9]{4}_", "", name)

    @staticmethod
    def crop(img, margin=0):
        """Обрезает белые поля."""
        try:
            rgb = img.convert("RGB")
            diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, (255, 255, 255)))
            box = diff.getbbox()
            if box:
                l = max(0, box[0] - margin)
                u = max(0, box[1] - margin)
                r = min(rgb.width, box[2] + margin)
                d = min(rgb.height, box[3] + margin)
                return img.crop((l, u, r, d))
        except Exception as e:
            log.warning("Обрезка не вышла: %s", e)
        return img

    @staticmethod
    def check_deps():
        """Проверяет libreoffice и pdftoppm."""
        miss = [c for c in ("libreoffice", "pdftoppm") if not shutil.which(c)]
        if miss:
            raise RuntimeError("Нет программ: " + ", ".join(miss))


class Converter:
    """Главный класс."""

    def __init__(self, cfg_path):
        """Читает конфиг и готовит пути."""
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)

        for k in ("source_dir", "output_dir", "json_path"):
            if k not in cfg:
                raise ValueError("Нет поля: " + k)

        self.src = os.path.abspath(cfg["source_dir"])
        self.out = os.path.abspath(cfg["output_dir"])
        self.db_path = os.path.abspath(cfg["json_path"])
        self.list_path = os.path.abspath(cfg.get("list_json_path")
                                         or os.path.splitext(self.db_path)[0] + "_list.json")
        self.workers = max(1, int(cfg.get("max_workers", 4)))
        self.dpi = int(cfg.get("png_dpi", 150))

        self.db = DB(self.db_path, self.list_path)
        self.files = Files()
        os.makedirs(self.out, exist_ok=True)
        self.stats = {"new": 0, "updated": 0, "unchanged": 0, "errors": 0, "deleted": 0}

    def base_name(self, path):
        """Имя картинки без хэша."""
        rel = os.path.relpath(path, self.src)
        noext, ext = os.path.splitext(rel)
        ext = ext.lower()
        target = ext if ext in IMGS else ".png"
        name = os.path.normpath(noext).replace(os.sep, "_")
        return f"{name}_{ext[1:] if ext else 'noext'}{target}"

    def image_name(self, path, h):
        """Имя картинки с хэшем."""
        return f"{h[:4]}_{self.base_name(path)}" if h else self.base_name(path)

    def find_old(self, base):
        """Ищет старую запись."""
        for name, info in self.db.data.items():
            if self.files.strip_hash(name) == base:
                return name, info
        return None, None

    def to_pdf(self, src, tmp):
        """Делает PDF через LibreOffice."""
        prof = os.path.join(tmp, "prof")
        os.makedirs(prof, exist_ok=True)
        ext = os.path.splitext(src)[1].lower()
        spec = ('pdf:calc_pdf_Export:{"SinglePageSheets":{"type":"boolean","value":"true"}}'
                if ext in (".xls", ".xlsx") else "pdf")
        cmd = ["libreoffice", "--headless", "--convert-to", spec,
               "--outdir", tmp, "-env:UserInstallation=file://" + os.path.abspath(prof), src]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode and spec != "pdf":
            cmd[3] = "pdf"
            r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError("LibreOffice: " + (r.stderr.strip() or r.stdout.strip() or "ошибка"))
        pdf = os.path.join(tmp, os.path.splitext(os.path.basename(src))[0] + ".pdf")
        if not os.path.isfile(pdf):
            raise RuntimeError("PDF не создан")
        return pdf

    def to_png(self, pdf, tmp, out_name):
        """Делает одну длинную PNG из PDF."""
        prefix = os.path.join(tmp, "page")
        r = subprocess.run(["pdftoppm", "-png", "-r", str(self.dpi), pdf, prefix],
                           capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError("pdftoppm: " + (r.stderr.strip() or r.stdout.strip() or "ошибка"))

        pages = [os.path.join(tmp, f) for f in os.listdir(tmp)
                 if f.startswith("page-") and f.endswith(".png")]
        pages.sort(key=lambda p: int(re.search(r"page-(\d+)", p).group(1)) if re.search(r"page-(\d+)", p) else 0)
        if not pages:
            raise RuntimeError("Нет страниц")

        imgs = [self.files.crop(Image.open(p)) for p in pages]
        w = max(i.width for i in imgs)
        h = sum(i.height for i in imgs)
        combo = Image.new("RGB", (w, h), (255, 255, 255))
        y = 0
        for i in imgs:
            combo.paste(i, ((w - i.width) // 2, y))
            y += i.height
            i.close()
        self.files.crop(combo, 20).save(os.path.join(self.out, out_name))
        return out_name

    def process(self, src, force=False):
        """Обрабатывает один файл."""
        h = self.files.sha256(src)
        base = self.base_name(src)
        old_name, info = self.find_old(base)
        old_h = info.get("hash", "") if isinstance(info, dict) else ""
        is_new = info is None

        if not (is_new or h != old_h or force):
            return "unchanged", src, {}, None

        new_name = self.image_name(src, h)
        old_del = old_name if old_name and old_name != new_name else None
        ext = os.path.splitext(src)[1].lower()

        if ext in IMGS:
            dst = os.path.join(self.out, new_name)
            shutil.copyfile(src, dst)
            with Image.open(dst) as im:
                self.files.crop(im, 20).save(dst)
        else:
            tmp = tempfile.mkdtemp(prefix="conv_", dir=self.out)
            try:
                pdf = self.to_pdf(src, tmp)
                new_name = self.to_png(pdf, tmp, new_name)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

        status = "new" if is_new else "updated"
        return status, src, {new_name: {"hash": h}}, old_del

    def scan(self):
        """Ищет все нужные файлы."""
        found = []
        for root, _, names in os.walk(self.src):
            for n in names:
                if n.startswith(("~$", ".")):
                    continue
                if os.path.splitext(n)[1].lower() in ALL:
                    found.append(os.path.abspath(os.path.join(root, n)))
        return found

    def clean(self, files):
        """Удаляет картинки пропавших файлов."""
        valid = {self.base_name(f) for f in files}
        for name in [n for n in self.db.data if self.files.strip_hash(n) not in valid]:
            self.db.remove([name], self.out)
            self.stats["deleted"] += 1
            log.info("Удалено: %s", name)

    def run(self, force=False):
        """Главный запуск."""
        if not os.path.isdir(self.src):
            raise NotADirectoryError("Нет папки: " + self.src)

        files = self.scan()
        if any(os.path.splitext(f)[1].lower() in DOCS for f in files):
            self.files.check_deps()

        log.info("Обход: %s", self.src)
        self.clean(set(files))
        log.info("Файлов: %d, потоков: %d", len(files), self.workers)

        with ThreadPoolExecutor(self.workers) as ex:
            futs = {ex.submit(self.process, f, force): f for f in files}
            for fut in as_completed(futs):
                src = futs[fut]
                try:
                    st, path, data, old = fut.result()
                    if old:
                        self.db.remove([old], self.out)
                    if st == "unchanged":
                        self.stats["unchanged"] += 1
                        log.info("Без изменений: %s", path)
                        continue
                    self.db.data.update(data)
                    self.stats[st] += 1
                    log.info("%s: %s", "Новый" if st == "new" else "Обновлён", path)
                except Exception as e:
                    self.stats["errors"] += 1
                    log.error("Ошибка %s: %s", src, e)

        self.db.save()
        log.info("JSON: %s", self.db_path)
        log.info("Список: %s", self.list_path)
        log.info("СТАТИСТИКА: new=%d, upd=%d, same=%d, err=%d, del=%d",
                 self.stats["new"], self.stats["updated"],
                 self.stats["unchanged"], self.stats["errors"], self.stats["deleted"])


class App:
    """Точка входа."""

    def __init__(self):
        """Читает аргументы командной строки."""
        p = argparse.ArgumentParser(description="Конвертер документов в PNG")
        p.add_argument("--config", default="config.json")
        p.add_argument("--force", action="store_true")
        self.args = p.parse_args()

    def run(self):
        """Создаёт конвертер и запускает."""
        try:
            Converter(self.args.config).run(self.args.force)
        except Exception as e:
            log.error("Ошибка: %s", e)
            sys.exit(1)


if __name__ == "__main__":
    App().run()

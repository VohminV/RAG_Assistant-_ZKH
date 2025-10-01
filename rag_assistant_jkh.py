import os
import re
import warnings
import numpy as np
import faiss
import json
import random
import time
from typing import List, Dict, Tuple, Optional, Type, Any
from pathlib import Path
from sentence_transformers import SentenceTransformer
import nltk
from nltk.tokenize import sent_tokenize
from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig
import torch
from torch.cuda.amp import autocast
import gradio as gr
from ddgs import DDGS
from functools import wraps
import psutil
torch.cuda.empty_cache()
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

warnings.filterwarnings('ignore')

try:
    nltk.data.find('tokenizers/punkt')
except LookupError:
    nltk.download('punkt', quiet=True)

STOP_WORDS = {
    "добрый", "день", "прошу", "пожалуйста", "меры", "примите", "здравствуйте",
    "спасибо", "вечер", "утро", "хочу", "напоминаю", "уведомляю"
}

# ---------------------------
# Загрузка данных
# ---------------------------

CHUNKS_PATH = "/jkh-data/document_chunks.json"
INDEX_PATH  = "/jkh-data/faiss_index.bin"

print("📥 Загрузка чанков...")
with open(CHUNKS_PATH, "r", encoding="utf-8") as f:
    chunks_data = json.load(f)
print(f"✅ Чанков загружено: {len(chunks_data)}")

print("📥 Загрузка FAISS-индекса...")
index = faiss.read_index(INDEX_PATH)
print(f"✅ Индекс загружен: {index.ntotal} векторов")

# ---------------------------
# Загрузка моделей
# ---------------------------
def get_cpu_info():
    """Возвращает информацию о CPU"""
    return {
        "model": psutil.cpu_freq().max if psutil.cpu_freq() else "N/A",
        "cores_physical": psutil.cpu_count(logical=False),
        "cores_total": psutil.cpu_count(logical=True),
        "brand": getattr(psutil, "_cpu_brand", "Unknown")  # не всегда доступно
    }

def get_all_gpu_info():
    """Возвращает использование памяти и загрузку по всем GPU"""
    if not torch.cuda.is_available():
        return None

    info = []
    for i in range(torch.cuda.device_count()):
        torch.cuda.set_device(i)
        allocated = torch.cuda.memory_allocated(i) / 1024**2
        reserved = torch.cuda.memory_reserved(i) / 1024**2
        max_allocated = torch.cuda.max_memory_allocated(i) / 1024**2
        name = torch.cuda.get_device_name(i)
        total_mem = torch.cuda.get_device_properties(i).total_memory / 1024**2

        info.append({
            "id": i,
            "name": name,
            "total_mb": total_mem,
            "allocated_mb": allocated,
            "reserved_mb": reserved,
            "max_allocated_mb": max_allocated,
        })
    return info

def monitor_resources(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        # --- CPU & RAM ---
        process = psutil.Process(os.getpid())
        ram_before = process.memory_info().rss / 1024**2
        cpu_percent_before = psutil.cpu_percent(interval=0.1)  # краткий сэмпл
        start_time = time.time()

        # --- GPU: сбросить пик по всем устройствам ---
        if torch.cuda.is_available():
            for i in range(torch.cuda.device_count()):
                torch.cuda.reset_peak_memory_stats(i)
                torch.cuda.empty_cache()  # опционально, для чистоты

        # --- Выполнение функции ---
        result = func(*args, **kwargs)

        # --- После выполнения ---
        elapsed = time.time() - start_time
        ram_after = process.memory_info().rss / 1024**2
        cpu_percent_after = psutil.cpu_percent(interval=0.1)

        # --- Вывод CPU ---
        cpu_info = get_cpu_info()
        print(f"\n{'='*60}")
        print(f"📊 Диагностика: {func.__name__}")
        print(f"⏱️  Время выполнения: {elapsed:.2f} сек")
        print(f"🧠 CPU: {cpu_info['cores_physical']} физ. / {cpu_info['cores_total']} лог. ядер")
        print(f"📈 Загрузка CPU: до {cpu_percent_before:.1f}% → после {cpu_percent_after:.1f}%")
        print(f"💾 RAM: {ram_before:.1f} → {ram_after:.1f} MB (+{ram_after - ram_before:.1f})")

        # --- Вывод GPU (все устройства) ---
        gpu_info = get_all_gpu_info()
        if gpu_info:
            print(f"\n🎮 GPU-устройства ({len(gpu_info)}):")
            for gpu in gpu_info:
                print(
                    f"  GPU {gpu['id']} ({gpu['name']}): "
                    f"alloc {gpu['allocated_mb']:.1f} MB | "
                    f"peak {gpu['max_allocated_mb']:.1f} MB | "
                    f"total {gpu['total_mb']:.0f} MB"
                )
        else:
            print("⚠️  GPU недоступен")

        print(f"{'='*60}\n")
        return result
    return wrapper
    
print("🧠 Загрузка моделей...")

print("📥 Загрузка модели ViktorZver/FRIDA...")
embedding_model = SentenceTransformer("ViktorZver/FRIDA", device=str(device))
print("✅ FRIDA загружена")

print("📥 Загрузка токенизатора...")
tokenizer = AutoTokenizer.from_pretrained("IlyaGusev/saiga_llama3_8b")
model_name = "IlyaGusev/saiga_llama3_8b"

if tokenizer.pad_token is None:
    tokenizer.pad_token = tokenizer.eos_token

def estimate_tokens(text: str) -> int:
    return len(tokenizer.encode(text, add_special_tokens=False))


bnb_config = BitsAndBytesConfig(
    load_in_8bit=True,
)


try:
    model = AutoModelForCausalLM.from_pretrained(
        model_name,
        quantization_config=bnb_config,
        device_map="balanced",
        trust_remote_code=False,
        attn_implementation="sdpa"
    )
    print(f"✅ LLM загружена в 4-bit на: {device}")
except Exception as e:
    print(f"❌ Ошибка загрузки модели: {e}")
    raise


# ---------------------------
# Базовый класс агента
# ---------------------------

class RAGAgent:
    def __init__(self, name: str, keywords: List[str]):
        self.name = name
        self.keywords = [kw.lower() for kw in keywords]
        self.feedback_data = []
        self.confidence_threshold = 0.7
        self.load_feedback()

    """def matches(self, query: str) -> bool:
        q = query.lower()
        # Извлекаем только целые слова
        words = set(re.findall(r'\b[а-яёa-z0-9]+\b', q))
        return any(kw in words for kw in self.keywords)"""
    def matches(self, query: str) -> bool:
        q = query.lower()
        # Проверяем, содержит ли запрос ЛЮБОЕ из ключевых слов как подстроку
        # Это делает систему устойчивой к опечаткам, склонениям и частичным совпадениям
        return any(kw in q for kw in self.keywords)
       
    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        raise NotImplementedError("Каждый агент должен реализовать свой _build_prompt")

    def get_role_instruction(self, role: str) -> str:
        base = {
            "житель": "Ответ ориентирован на жителя. Давайте пошаговые действия с ссылками на НПА.",
            "исполнитель": "Ответ ориентирован на УК/ТСН. Включайте судебную практику и процедуры.",
            "смешанная": "Разделите ответ на две части: для жителя и для исполнителя."
        }
        return base.get(role, base["смешанная"])

    # ---- Обучение агента ----
    def add_feedback(self, query: str, ideal_answer: str, rating: float = 1.0):
        if rating >= 0.8:
            self.feedback_data.append({
                "query": query,
                "ideal_answer": ideal_answer,
                "rating": rating,
                "timestamp": time.time()
            })
        self._save_feedback()

    def _save_feedback(self):
        feedback_file = f"agent_feedback_{self.name.replace(' ', '_')}.json"
        with open(feedback_file, "w", encoding="utf-8") as f:
            json.dump(self.feedback_data, f, ensure_ascii=False, indent=2)

    def load_feedback(self):
        feedback_file = f"agent_feedback_{self.name.replace(' ', '_')}.json"
        if os.path.exists(feedback_file):
            try:
                with open(feedback_file, "r", encoding="utf-8") as f:
                    self.feedback_data = json.load(f)
            except:
                self.feedback_data = []

    def improve_prompt_from_feedback(self) -> str:
        if len(self.feedback_data) < 3:
            return ""
        examples = random.sample(self.feedback_data, min(3, len(self.feedback_data)))
        instruction = "\n\nНа основе успешных примеров, улучши стиль и структуру ответа:\n"
        for ex in examples:
            instruction += f"Вопрос: {ex['query']}\nОтвет: {ex['ideal_answer']}\n---\n"
        return instruction

    # ---- Мультиагентность: запрос к другому агенту ----
    def consult_other_agent(self, query: str, rag_system) -> str:
        """Запрашивает контекст у другого агента через MetaAgent"""
        other_agent = rag_system.meta_agent.route(query, exclude_agent=self)
        if other_agent:
            print(f"🤝 {self.name} запрашивает помощь у {other_agent.name}")
            context = rag_system.generate_context_for_agent(query, other_agent, rag_system.detect_user_role(query))
            return f"\n\n[Консультация от агента '{other_agent.name}']: {context}\n"
        return ""


# ---------------------------
# Конкретные агенты
# ---------------------------

class TariffAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Тарифы и начисления", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "тариф": {
                "synonyms": ["стоимость", "цена", "плата", "надбавка", "индекс роста"],
                "norm_refs": ["ЖК РФ, ст. 154", "ПП РФ №354, раздел 4"],
                "contexts": ["расчет", "повышение", "региональный"]
            },
            "начисление": {
                "synonyms": ["расчет", "оплата", "формирование платежа", "доначисление"],
                "norm_refs": ["ПП РФ №354, раздел 5"],
                "contexts": ["по нормативу", "по счетчику", "по среднему", "корректировка"]
            },
            "оплата": {
                "synonyms": ["платёж", "перечисление", "взнос", "квитанция", "платёжка"],
                "norm_refs": ["ФЗ №177-ФЗ", "ПП РФ №354, п. 69"],
                "contexts": ["срок", "пени", "рассрочка", "комиссия"]
            },
            "перерасчет": {
                "synonyms": ["перерасчёт", "корректировка", "доначисление", "пересчет"],
                "norm_refs": ["ПП РФ №354, п. 86-90"],
                "contexts": ["временное отсутствие", "некачество услуги", "поверка счетчика", "акт сверки"]
            },
            "повышающий коэффициент": {
                "synonyms": ["коэффициент 1.5", "надбавка", "повышающий множитель"],
                "norm_refs": ["ПП РФ №354, п. 42(1)"],
                "contexts": {
                    "отсутствие_ипу": "Применяется при отсутствии ИПУ.",
                    "недопуск_к_поверке": "Применяется при отказе в допуске к поверке."
                }
            },
            "ипу": {
                "synonyms": ["индивидуальный прибор учета", "счетчик", "водомер", "электросчетчик"],
                "norm_refs": ["ПП РФ №354, раздел 5"],
                "contexts": ["установка", "поверка", "истёк срок", "акт поверки"]
            },
            "одпу": {
                "synonyms": ["общедомовой прибор учета", "общедомовой счетчик"],
                "norm_refs": ["ПП РФ №354, раздел 5"],
                "contexts": ["выход из строя", "отсутствие", "расчет по среднему"]
            },
            "одн": {
                "synonyms": ["общедомовые нужды", "КР на СОИ", "коммунальный ресурс на СОИ"],
                "norm_refs": ["ПП РФ №354, раздел 9", "ПП РФ №491"],
                "contexts": ["расчет", "перерасчет", "почему платим"]
            },
            "срок поверки": {
                "synonyms": ["истёк срок поверки", "просроченная поверка", "акт поверки"],
                "norm_refs": ["ФЗ №102-ФЗ", "ПП РФ №354, п. 81(12)"],
                "contexts": ["перерасчет", "начисление по нормативу", "штраф"]
            },
            "временное отсутствие": {
                "synonyms": ["отпуск", "командировка", "уехал", "отсутствие более 5 дней"],
                "norm_refs": ["ПП РФ №354, п. 86"],
                "contexts": ["перерасчет", "документы", "максимум 6 месяцев"]
            },
            "документы для перерасчета": {
                "synonyms": ["билеты", "справки", "командировочное удостоверение", "акт об отсутствии"],
                "norm_refs": ["ПП РФ №354, п. 90"],
                "contexts": ["подача в течение 30 дней", "срок рассмотрения 5 дней"]
            },
            "пени": {
                "synonyms": ["штраф", "неустойка", "процент за просрочку"],
                "norm_refs": ["ЖК РФ, ст. 155", "ПП РФ №329"],
                "contexts": ["расчет по ключевой ставке", "льготы", "рассрочка"]
            },
            "продажа квартиры": {
                "synonyms": ["акт сверки счетчиков", "документ перед продажей", "услуга перед продажей"],
                "norm_refs": ["ЖК РФ, ст. 153"],
                "contexts": ["обязательность", "стоимость акта", "передача показаний"]
            },
            "плановое отключение": {
                "synonyms": ["профилактические работы", "отключение на 14 суток", "ремонт"],
                "norm_refs": ["ПП РФ №354, п. 98"],
                "contexts": ["уведомление за 10 дней", "перерасчет не положен"]
            },
            "судебная практика": {
                "synonyms": ["решение суда", "определение ВС РФ", "позиция суда"],
                "norm_refs": [],
                "contexts": ["прецеденты", "успешные иски", "отказы"]
            },
            "региональный тариф": {
                "synonyms": ["тариф по региону", "местный тариф", "ФГИС Тариф"],
                "norm_refs": ["ФЗ №210-ФЗ", "ПП РФ №1149"],
                "contexts": ["приоритет над федеральным", "сайт ФАС", "с 2026 года"]
            },
            "гвс": {"synonyms": ["горячее водоснабжение"], "norm_refs": ["ПП РФ №354, п. 40"], "contexts": []},
            "кубометр": {"synonyms": ["м3", "объём"], "norm_refs": [], "contexts": []},
            "акт сверки": {"synonyms": ["акт сверки показаний", "акт передачи"], "norm_refs": [], "contexts": []},
            "не совпадает сумма": {"synonyms": ["ошибка в квитанции", "долг в квитанции", "задвоили оплату"], "norm_refs": [], "contexts": []},
            "комиссия банка": {"synonyms": ["плата за оплату", "комиссия за перевод"], "norm_refs": [], "contexts": []},
            "почему за лифт": {"synonyms": ["содержание общего имущества", "содержание МКД"], "norm_refs": ["ЖК РФ, ст. 154"], "contexts": []},
            "переплата": {"synonyms": ["излишне уплаченная сумма", "возврат средств"], "norm_refs": ["ПП РФ №354, п. 35"], "contexts": []},
            "не пришла оплата": {"synonyms": ["оплата не зачтена", "где долг", "техническая ошибка"], "norm_refs": [], "contexts": []},
            "неправильный тариф": {"synonyms": ["не соответствует региональному", "повышение тарифа", "обоснование тарифа"], "norm_refs": [], "contexts": []},
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ДО формирования промпта, а не внутри него.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы на основе терминов
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            # Пропускаем чёрный список
                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            # Оцениваем вес источника
                            weight = 0
                            if any(official in domain for official in OFFICIAL_DOMAINS):
                                weight = 3  # Официальный источник
                            elif any(gov in domain for gov in [".gov.ru", ".gkh.ru"]):
                                weight = 2  # Государственный портал
                            else:
                                weight = 1  # Обычный источник

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    # Сортируем по весу и убираем дубликаты
                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])  # Хешируем начало сниппета
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(
                                f"{prefix}• {r['body']}\n  Источник: {r['href']}\n"
                            )
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        # Добавляем ключевые нормативные акты
        queries.append(f"{query} ПП РФ 354")
        queries.append(f"{query} ЖК РФ")
        queries.append(f"{query} судебная практика")
        queries.append(f"{query} региональный тариф")
        # Добавляем синонимы из словаря
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:  # Берем первые 2 синонима
                    queries.append(query.replace(term, synonym))
        return list(set(queries))  # Убираем дубликаты

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Формирует системный промт для агента 'Тарифы и начисления'.
        Итоговый ответ: краткий вывод + ссылки на законы и постановления из контекста.
        """
        # Динамические обновления (например, свежие постановления по тарифам)
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        # Проверка, нужен ли расчёт пени
        penalty_keywords = ["пени", "пеня", "неустойка", "штраф за просрочку", "ставка цб", "9.5%", "ключевая ставка"]
        q_lower = summary.lower()
        should_calculate_penalty = any(kw in q_lower for kw in penalty_keywords)
    
        # Системный промт
        system_prompt = (
            "Ты — экспертный ИИ-ассистент по тарифам и начислениям в сфере ЖКХ. "
            "Отвечай строго по закону, без выдуманных данных. "
            "Структура ответа всегда: краткий вывод → нормативная база.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. НИКАКИХ ГАЛЛЮЦИНАЦИЙ: если информации нет, ответь: 'Недостаточно данных для точного ответа. Обратитесь в управляющую компанию.'\n"
            "2. В кратком выводе — 2–3 предложения по сути.\n"
            "3. В нормативной базе перечисляй только реально найденные законы, статьи ЖК РФ, ФЗ или постановления (с номерами и датами).\n"
            "4. Не добавляй формулы и примеры, если пользователь не спрашивал про расчёт.\n\n"
            f"### Контекстная информация:\n{context_text}\n\n"
            f"### Результаты веб-поиска:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
        )
    
        # Если вопрос про пени — добавляем блок с формулой
        if should_calculate_penalty:
            system_prompt += (
                "\n**Если в вопросе упомянуты пени — добавь формулу:**\n"
                "- Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Основание: ЖК РФ ст. 155.1, ПП РФ №354, ПП РФ №329.\n"
                "- Начало начисления: с 31-го дня после срока оплаты.\n"
            )
    
        # Оборачиваем в формат Saiga/LLaMA-3
        system_prompt_formatted = (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )

        return system_prompt_formatted

class NormativeAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Нормативные документы", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "закон": {
                "synonyms": ["фз", "федеральный закон", "нормативный акт", "правовой акт"],
                "norm_refs": [],
                "contexts": ["жилищный", "коммунальный", "гражданский"]
            },
            "пп рф": {
                "synonyms": ["постановление правительства", "постановление", "нормативный документ"],
                "norm_refs": ["ПП РФ №354", "ПП РФ №491", "ПП РФ №329"],
                "contexts": ["расчет", "тарифы", "обязанности", "перерасчет"]
            },
            "норматив": {
                "synonyms": ["норма потребления", "расчетная норма", "лимит", "объём по нормативу"],
                "norm_refs": ["ПП РФ №354, п. 21", "ПП РФ №491"],
                "contexts": ["установление", "пересмотр", "дифференцированный", "сезонный"]
            },
            "право": {
                "synonyms": ["права жильцов", "обязанности УК", "законные основания", "юридические гарантии"],
                "norm_refs": ["ЖК РФ, ст. 153-160", "ФЗ №59-ФЗ"],
                "contexts": ["защита прав", "жалобы", "судебная защита"]
            },
            "регламент": {
                "synonyms": ["правила", "порядок", "инструкция", "методика"],
                "norm_refs": ["ПП РФ №354", "ПП РФ №491"],
                "contexts": ["расчет", "предоставление услуг", "качество", "перерасчет"]
            },
            "жилищный кодекс": {
                "synonyms": ["жк рф", "жилищное законодательство", "жилищные права"],
                "norm_refs": ["ЖК РФ, ст. 153-169"],
                "contexts": ["обязанности", "платежи", "управляющие компании", "собственники"]
            },
            "разъяснения минстроя": {
                "synonyms": ["письма минстроя", "разъяснения", "официальные комментарии"],
                "norm_refs": [],
                "contexts": ["толкование норм", "практика применения", "спорные вопросы"]
            },
            "письма ростехнадзора": {
                "synonyms": ["разъяснения ростехнадзора", "надзорные акты", "контроль"],
                "norm_refs": [],
                "contexts": ["безопасность", "техническое состояние", "поверка", "допуск"]
            },
            "разъяснения фас": {
                "synonyms": ["антимонопольные разъяснения", "тарифы", "регулирование цен"],
                "norm_refs": [],
                "contexts": ["надбавки", "обоснование тарифов", "региональные органы"]
            },
            "постановление пленума вс рф": {
                "synonyms": ["судебная практика", "разъяснения судов", "позиция верховного суда"],
                "norm_refs": [],
                "contexts": ["прецеденты", "толкование законов", "единая практика"]
            },
            "определение конституционного суда рф": {
                "synonyms": ["конституционность", "основной закон", "защита прав"],
                "norm_refs": [],
                "contexts": ["соответствие законов конституции", "жалобы граждан"]
            },
            "международные конвенции": {
                "synonyms": ["европейская практика", "европейский суд", "права человека"],
                "norm_refs": [],
                "contexts": ["энергоэффективность", "право на жилище", "экологические стандарты"]
            },
            "где прописано": {
                "synonyms": ["сошлитесь на закон", "по закону", "нормативное обоснование"],
                "norm_refs": [],
                "contexts": ["требование ссылки", "юридическая аргументация"]
            },
            "обязанности ук": {
                "synonyms": ["обязанности управляющей компании", "ответственность", "предоставление информации"],
                "norm_refs": ["ЖК РФ, ст. 161-165", "ПП РФ №354"],
                "contexts": ["расчеты", "отчетность", "доступ к документам"]
            },
            "методические рекомендации": {
                "synonyms": ["методички", "руководства", "инструкции для специалистов"],
                "norm_refs": [],
                "contexts": ["расчеты", "документооборот", "взаимодействие с жильцами"]
            },
            "обзор практики": {
                "synonyms": ["анализ судебных решений", "обобщение практики", "статистика судов"],
                "norm_refs": [],
                "contexts": ["верховный суд", "апелляционные суды", "арбитраж"]
            },
            "общая площадь": {
                "synonyms": ["Sобщ", "площадь квартиры", "квартира общая","общей площади"],
                "norm_refs": ["ЖК РФ, ст. 16", "ЖК РФ, ст. 17"],
                "contexts": ["расчёт площади", "жилая и нежилая площадь"]
            },
            "жилая площадь": {
                "synonyms": ["Sжил", "площадь жилого помещения", "квартира жилая", "квартира жилая", "жилой площади"],
                "norm_refs": ["ЖК РФ, ст. 16", "ЖК РФ, ст. 17"],
                "contexts": ["расчёт площади", "жилая и нежилая площадь"]
            },
            "долевая собственность": {
                "synonyms": ["общая долевая собственность", "совместная долевая собственность", "доли жильцов"],
                "norm_refs": ["ЖК РФ, ст. 245", "ЖК РФ, ст. 246"],
                "contexts": ["права и обязанности собственников", "управление общим имуществом", "ограничения и распоряжение"]
            }
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "ksrf.ru", "vsrf.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".ksrf.ru", ".vsrf.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ПП РФ 354")
        queries.append(f"{query} ЖК РФ")
        queries.append(f"{query} Минстрой России разъяснения")
        queries.append(f"{query} судебная практика ВС РФ")
        queries.append(f"{query} Конституционный Суд РФ")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Нормативные документы (ЖКХ).
        Формирует системный промт для Saiga/LLaMA-3 8B:
        - Ответ = краткий вывод + ссылки на законы и постановления
        - Приоритет официальных источников
        - Никаких галлюцинаций
        - Формулы пени только при запросе
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        # Проверка на упоминание пени
        penalty_keywords = ["пени", "пеня", "неустойка", "штраф за просрочку", "ставка цб", "9.5%", "ключевая ставка"]
        q_lower = summary.lower()
        should_calculate_penalty = any(kw in q_lower for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — эксперт по нормативным документам в сфере ЖКХ. "
            "Давай только точные ответы на основе контекста и найденных законов.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если нет данных — ответ: 'Недостаточно данных для точного ответа.'\n"
            "2. Все утверждения сопровождай ссылками на законы и постановления (ЖК РФ, ФЗ, ПП РФ).\n"
            "3. Структура ответа фиксирована: \n"
            "   - Краткий вывод (2–3 предложения)\n"
            "   - Нормативная база (списком, только из найденного контента)\n"
            "4. Никаких длинных пояснений и 'портянок'.\n"
            "5. Формулы пени — только если они прямо упомянуты в вопросе.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Обновления:\n{extra}\n\n"
        )
    
        # Добавляем формулу пени при необходимости
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчёт пени:**\n"
                "- Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Основание: ЖК РФ ст. 155.1, ПП РФ №354, ПП РФ №329\n"
                "- Начало начисления: с 31-го дня после срока оплаты.\n"
            )
    
        # Обертка для Saiga/LLaMA-3
        system_prompt_formatted = (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )
    
        return system_prompt_formatted

class TechnicalAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Технические регламенты", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "температура": {
                "synonyms": ["норма отопления", "холодно в квартире", "жарко", "перегрев", "замер температуры"],
                "norm_refs": ["СанПиН 1.2.3685-21, п. 9.2", "ПП РФ №354, п. 54"],
                "contexts": ["отопление", "горячая вода", "воздух в помещении"]
            },
            "давление": {
                "synonyms": ["напор", "слабый напор", "давление на вводе", "гидравлический удар"],
                "norm_refs": ["СанПиН 1.2.3685-21, п. 9.4", "ПП РФ №354, п. 54(1)"],
                "contexts": ["водоснабжение", "ХВС", "ГВС", "циркуляция"]
            },
            "лифт": {
                "synonyms": ["гудит лифт", "сломался лифт", "не запустился", "техническое обслуживание лифта"],
                "norm_refs": ["ПП РФ №354, п. 54(12)", "Правила технической эксплуатации ЖКХ"],
                "contexts": ["шум", "остановка", "ремонт", "график ТО"]
            },
            "не греет": {
                "synonyms": ["холодный", "греет плохо", "батарея холодная", "радиатор не работает", "воздух в батарее"],
                "norm_refs": ["ПП РФ №354, п. 54(2)", "СанПиН 1.2.3685-21, п. 9.2"],
                "contexts": ["завоздушивание", "засор", "стояк", "ИТП", "циркуляционный насос"]
            },
            "протечка": {
                "synonyms": ["засор", "течь", "авария", "подтопление", "влажность"],
                "norm_refs": ["ПП РФ №354, п. 59", "Правила технической эксплуатации ЖКХ"],
                "contexts": ["труба", "стояк", "сантехника", "регресс к УО"]
            },
            "шум": {
                "synonyms": ["гудит", "вибрация", "стук", "шум в подвале", "воняет в подъезде"],
                "norm_refs": ["СанПиН 1.2.3685-21, п. 8.3", "ПП РФ №354, п. 54(12)"],
                "contexts": ["лифт", "насос", "тепловой пункт", "вентиляция"]
            },
            "вода": {
                "synonyms": ["нет горячей воды", "нет холодной воды", "перегрев воды", "температура воды", "качество воды"],
                "norm_refs": ["СанПиН 1.2.3685-21, п. 9.3-9.4", "ПП РФ №354, п. 54(1)"],
                "contexts": ["ГВС", "ХВС", "норма", "замер", "отключение"]
            },
            "снип": {
                "synonyms": ["гост", "технические условия", "строительные нормы", "правила проектирования"],
                "norm_refs": [],
                "contexts": ["проектирование", "монтаж", "приемка", "эксплуатация"]
            },
            "санпин": {
                "synonyms": ["гигиенические нормы", "нормативная температура", "безопасность среды обитания"],
                "norm_refs": ["СанПиН 1.2.3685-21"],
                "contexts": ["отопление", "вода", "воздух", "шум", "освещение"]
            },
            "итп": {
                "synonyms": ["индивидуальный тепловой пункт", "тепловой пункт", "циркуляционный насос", "трехтрубная система"],
                "norm_refs": ["Правила технической эксплуатации ЖКХ", "ПП РФ №354"],
                "contexts": ["регулирование", "температура", "давление", "неисправность"]
            },
            "завоздушивание": {
                "synonyms": ["воздух в системе", "воздух в батарее", "не циркулирует", "холодные участки"],
                "norm_refs": ["Правила технической эксплуатации ЖКХ"],
                "contexts": ["отопление", "радиаторы", "стояки", "спуск воздуха"]
            },
            "регресс к уо": {
                "synonyms": ["возмещение ущерба", "претензия к управляющей компании", "ответственность УО"],
                "norm_refs": ["ЖК РФ, ст. 161", "ПП РФ №354, п. 59(5)"],
                "contexts": ["протечка", "авария", "ущерб имуществу", "акт о заливе"]
            },
            "полотенцесушитель": {
                "synonyms": ["полотенчик", "сушилка", "не греет полотенцесушитель", "холодный полотенцесушитель"],
                "norm_refs": ["СанПиН 1.2.3685-21, п. 9.2", "ПП РФ №354, п. 54(2)"],
                "contexts": ["ГВС", "отопление", "стояк", "ремонт"]
            },
            "стояк": {
                "synonyms": ["вертикальная труба", "магистраль", "трубопровод", "коррозия стояка"],
                "norm_refs": ["Правила технической эксплуатации ЖКХ", "ПП РФ №491"],
                "contexts": ["замена", "протечка", "давление", "циркуляция"]
            },
            "норма": {
                "synonyms": ["норматив", "допустимое значение", "предельное значение", "гигиенический норматив"],
                "norm_refs": ["СанПиН 1.2.3685-21", "ПП РФ №354"],
                "contexts": ["температура", "давление", "шум", "освещенность", "влажность"]
            },
            "замер температуры": {
                "synonyms": ["акт замера", "фактическая температура", "проверка качества", "инструментальный контроль"],
                "norm_refs": ["ПП РФ №354, п. 58", "СанПиН 1.2.3685-21"],
                "contexts": ["отопление", "горячая вода", "жалоба", "перерасчет"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "rospotrebnadzor.ru", "rosconsumnadzor.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".rospotrebnadzor.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} СанПиН 1.2.3685-21")
        queries.append(f"{query} ПП РФ 354 раздел 6")
        queries.append(f"{query} Правила технической эксплуатации ЖКХ")
        queries.append(f"{query} норматив температуры отопления")
        queries.append(f"{query} давление воды норма")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Технические регламенты (ЖКХ).
        Формирует системный промт для Saiga/LLaMA-3 8B:
        - Ответ = краткий вывод + нормативные требования
        - Приоритет официальных источников (СанПиН, ПП РФ, Правила эксплуатации)
        - Жесткая структура, никаких "портянок"
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        # Проверка на упоминание пени (редко, но оставим)
        penalty_keywords = ["пени", "пеня", "неустойка", "штраф за просрочку", "ставка цб", "9.5%", "ключевая ставка"]
        q_lower = summary.lower()
        should_calculate_penalty = any(kw in q_lower for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — эксперт по техническому обслуживанию и качеству коммунальных услуг. "
            "Давай ответы только на основе официальных норм (СанПиН, ПП РФ, Правила эксплуатации).\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если данных нет — ответ: 'Недостаточно данных для точного ответа.'\n"
            "2. Все утверждения обязательно подкрепляй ссылками (напр. [ПП РФ №354, п. 59], [СанПиН 1.2.3685-21, п. 3.4]).\n"
            "3. Структура ответа фиксирована: \n"
            "   - Краткий вывод (2–3 предложения)\n"
            "   - Нормативные требования (списком, с точными пунктами)\n"
            "   - Порядок действий (акт, замеры, обращение, сроки, перерасчет)\n"
            "4. Никаких длинных рассуждений.\n"
            "5. Формулы пени — только если прямо спросят.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Обновления:\n{extra}\n\n"
        )
    
        # Блок пени (если требуется)
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчет пени (только при запросе):**\n"
                "- Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Основание: ЖК РФ ст. 155.1, ПП РФ №354, ПП РФ №329\n"
                "- Начало начисления: с 31-го дня после срока оплаты.\n"
            )
    
        # Справочник нормативки
        system_prompt += (
            "\n### Ключевые акты (для справки):\n"
            "- СанПиН 1.2.3685-21 (гигиенические требования)\n"
            "- ПП РФ №354 (раздел 6 — качество коммунальных услуг)\n"
            "- ПП РФ №491 (содержание общего имущества)\n"
            "- Правила технической эксплуатации жилищного фонда (Минстрой РФ)\n\n"
            f"{self.get_role_instruction(role)}"
        )
    
        # Обертка для Saiga/LLaMA-3
        system_prompt_formatted = (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )
    
        return system_prompt_formatted

class MeterAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Приборы учёта", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "счётчик": {
                "synonyms": ["пу", "ипу", "одпу", "водомер", "электросчётчик", "газовый счётчик"],
                "norm_refs": ["ФЗ №261-ФЗ, ст. 13", "ПП РФ №354, раздел 5"],
                "contexts": ["установка", "замена", "поверка", "передача показаний"]
            },
            "показания": {
                "synonyms": ["реальные показания", "передать показания", "куда передать", "ошибка в показаниях", "дистанционная передача"],
                "norm_refs": ["ПП РФ №354, п. 31(1)", "Правила учета КР"],
                "contexts": ["ежемесячная передача", "автоматическая передача", "сроки", "последствия не передачи"]
            },
            "поверка": {
                "synonyms": ["срок поверки", "истёк срок поверки", "акт поверки", "результаты поверки", "поверочный интервал"],
                "norm_refs": ["ФЗ №102-ФЗ", "ПП РФ №354, п. 81"],
                "contexts": ["перерасчет после поверки", "начисление по нормативу", "ответственность за просрочку"]
            },
            "замена": {
                "synonyms": ["не работает счётчик", "демонтаж счетчика", "установка нового", "техническая невозможность замены"],
                "norm_refs": ["ПП РФ №354, п. 31(5)", "ФЗ №261-ФЗ, ст. 13"],
                "contexts": ["кто должен менять", "за чей счёт", "акт обследования", "непригодность к эксплуатации"]
            },
            "опломбировка": {
                "synonyms": ["пломбировка", "опечатывание", "допуск к эксплуатации", "ввод в эксплуатацию"],
                "norm_refs": ["ПП РФ №354, п. 31(3)", "Правила учета КР"],
                "contexts": ["обязанность УК", "сроки опломбировки", "отказ в опломбировке", "штраф за самостоятельную опломбировку"]
            },
            "техническая невозможность": {
                "synonyms": ["невозможность установки", "акт обследования", "отсутствие места", "ветхое состояние труб"],
                "norm_refs": ["ПП РФ №354, п. 85", "Приказ Минстроя №XXX"],
                "contexts": ["процедура оформления", "подписание акта", "начисление по нормативу без коэффициента"]
            },
            "дистанционная передача": {
                "synonyms": ["автоматическая передача", "умный счётчик", "телеметрия", "интеллектуальная система учета"],
                "norm_refs": ["ФЗ №261-ФЗ, ст. 13(5)", "ПП РФ №354, п. 31(1)"],
                "contexts": ["обязательность с 2025 года", "совместимость", "стоимость установки", "передача без участия жильца"]
            },
            "электронный счетчик": {
                "synonyms": ["умный счетчик", "цифровой счетчик", "с дисплеем", "с радиомодулем"],
                "norm_refs": ["ФЗ №261-ФЗ", "ПП РФ №354"],
                "contexts": ["преимущества", "срок службы", "требования к классу точности"]
            },
            "механический счетчик": {
                "synonyms": ["крыльчатый", "старого образца", "без дисплея"],
                "norm_refs": ["ФЗ №102-ФЗ", "ПП РФ №354"],
                "contexts": ["допустимость", "срок поверки", "замена на электронный"]
            },
            "кто должен менять": {
                "synonyms": ["за чей счёт", "обязанность собственника", "обязанность УК", "фонд капремонта"],
                "norm_refs": ["ЖК РФ, ст. 158", "ПП РФ №354, п. 31(5)"],
                "contexts": ["истек срок службы", "выход из строя", "повреждение", "утеря пломбы"]
            },
            "акт обследования": {
                "synonyms": ["акт о невозможности установки", "комиссия", "обследование помещения", "технический акт"],
                "norm_refs": ["ПП РФ №354, п. 85", "Приказ Минстроя"],
                "contexts": ["состав комиссии", "образец акта", "срок действия", "обжалование"]
            },
            "отказ в опломбировке": {
                "synonyms": ["не опломбировали", "отказали в допуске", "не приняли счётчик", "требуют замены"],
                "norm_refs": ["ПП РФ №354, п. 31(3)", "ФЗ №261-ФЗ"],
                "contexts": ["законные основания", "жалоба в ГЖИ", "судебная практика", "начисление с коэффициентом"]
            },
            "истёк срок поверки": {
                "synonyms": ["просроченная поверка", "не прошёл поверку", "счётчик не действителен"],
                "norm_refs": ["ПП РФ №354, п. 81(12)", "ФЗ №102-ФЗ"],
                "contexts": ["начисление по нормативу с коэффициентом 1.5", "перерасчет после поверки", "ответственность за просрочку"]
            },
            "передать показания": {
                "synonyms": ["подать показания", "сообщить показания", "отправить данные", "через госуслуги"],
                "norm_refs": ["ПП РФ №354, п. 31(1)", "Правила учета КР"],
                "contexts": ["сроки (с 23 по 25 число)", "способы (лично, онлайн, по телефону)", "последствия не передачи"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "rostech.ru", "rosaccred.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".rostech.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ФЗ 261")
        queries.append(f"{query} ПП РФ 354 раздел 5")
        queries.append(f"{query} поверка счетчиков")
        queries.append(f"{query} техническая невозможность установки ИПУ")
        queries.append(f"{query} правила учета коммунальных ресурсов Минстрой")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Приборы учета.
        Формирует системный промт для Saiga/LLaMA-3 8B:
        - Фокус: ИПУ/ОДПУ (обязанности, установка, поверка, передача показаний, последствия).
        - Жесткая структура.
        - Только официальные источники (ФЗ, ПП РФ, приказы Минстроя/Ростехнадзора).
        Вопрос пользователя добавляется отдельно в generate_answer_chat.
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        penalty_keywords = ["пени", "пеня", "неустойка", "штраф за просрочку", "ставка цб", "9.5%", "ключевая ставка"]
        q_lower = summary.lower()
        should_calculate_penalty = any(kw in q_lower for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — эксперт по приборам учета коммунальных ресурсов. "
            "Дай точный, юридически обоснованный и структурированный ответ, "
            "используя ТОЛЬКО контекст, найденные законы и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если данных нет — отвечай: 'Недостаточно данных для точного ответа.'\n"
            "2. Все выводы подтверждай ссылками ([ФЗ №261-ФЗ, ст. 13], [ПП РФ №354, п. 31], [ФЗ №102-ФЗ, ст. 8]).\n"
            "3. Соблюдай структуру ответа.\n"
            "4. Формулы пени только если прямо спросят.\n"
            "5. Приоритет источников: ФЗ > ПП РФ > Приказы Минстроя/Ростехнадзора > разъяснения.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "### Структура ответа:\n"
            "- Краткий вывод\n"
            "- Нормативное обоснование (точные статьи/пункты)\n"
            "- Пошаговая инструкция (установка, поверка, передача показаний, ответственность, последствия)\n"
            "- Судебная практика\n"
        )
    
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчет пени (актуальная формула):**\n"
                "- Формула: Пени = Долг × Дни просрочки × (Ключевая ставка ЦБ / 300 / 100)\n"
                "- База: [ЖК РФ, ст. 155.1]\n"
                "- Ограничение: ≤ 9.5% годовых [ФЗ №44-ФЗ, ПП РФ №329]\n"
                "- Начало начисления: с 31-го дня после срока оплаты.\n"
            )
    
        system_prompt += (
            "\n### Ключевые нормативные акты (справочно):\n"
            "- ФЗ №261-ФЗ «Об энергосбережении» (ст. 13 — обязанность установки ИПУ)\n"
            "- ФЗ №102-ФЗ «Об обеспечении единства измерений» (поверка)\n"
            "- ПП РФ №354 (раздел 5 — расчёт, раздел 31 — приборы учета)\n"
            "- ПП РФ №491 (общее имущество, ОДПУ)\n"
            "- Приказы Минстроя и Ростехнадзора (акты обследования, поверка, замена)\n\n"
            f"{self.get_role_instruction(role)}"
        )
    
        return (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )


class DebtAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Задолженности", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "долг": {
                "synonyms": ["задолженность", "неуплата", "просрочка", "непогашенный платёж", "ареарс"],
                "norm_refs": ["ЖК РФ, ст. 155", "ПП РФ №354, п. 69"],
                "contexts": ["причины", "взыскание", "списание", "ошибочное начисление"]
            },
            "пени": {
                "synonyms": ["неустойка", "штраф за просрочку", "проценты за просрочку", "финансовая санкция"],
                "norm_refs": ["ЖК РФ, ст. 155.1", "ПП РФ №329"],
                "contexts": ["расчет", "формула", "ставка ЦБ", "лимит 9.5%", "до 2027 года"]
            },
            "ключевая ставка": {
                "synonyms": ["ставка цб", "9.5%", "процентная ставка", "минимальная ставка", "ставка 9.5 процентов"],
                "norm_refs": ["ЖК РФ, ст. 155.1", "ФЗ №44-ФЗ", "ПП РФ №329"],
                "contexts": ["расчет пени", "ограничение до 2027", "27 февраля 2022", "ФЗ №307-ФЗ"]
            },
            "рассрочка": {
                "synonyms": ["отсрочка", "план погашения", "график платежей", "соглашение о погашении"],
                "norm_refs": ["ЖК РФ, ст. 155.1(6)", "ПП РФ №354, п. 69(2)"],
                "contexts": ["условия предоставления", "заявление", "льготы", "субсидии"]
            },
            "взыскание": {
                "synonyms": ["суд за долг", "коллекторы", "исковое заявление", "приказное производство", "судебный приказ"],
                "norm_refs": ["ЖК РФ, ст. 158", "ГПК РФ, гл. 11"],
                "contexts": ["досудебное уведомление", "сроки давности", "моральный вред", "исполнительное производство"]
            },
            "ограничение услуги": {
                "synonyms": ["отключение за неуплату", "приостановка коммунальных услуг", "запрет на выезд", "арест имущества"],
                "norm_refs": ["ЖК РФ, ст. 158.1", "ПП РФ №354, п. 118"],
                "contexts": ["уведомление за 30 дней", "неполное ограничение", "запрещённые услуги (отопление, холодная вода)"]
            },
            "списание долга": {
                "synonyms": ["оплатил но долг", "почему долг", "технический долг", "ошибка в квитанции", "дублирование платежа"],
                "norm_refs": ["ЖК РФ, ст. 153", "ПП РФ №354, п. 35"],
                "contexts": ["акт сверки", "заявление на перерасчет", "жалоба в УК/ЕИРЦ", "сроки исправления"]
            },
            "формула пени": {
                "synonyms": ["как рассчитать пени", "расчет пени", "размер пени", "пени за просрочку", "математическая формула"],
                "norm_refs": ["ЖК РФ, ст. 155.1(1)"],
                "contexts": ["пример расчета", "калькулятор", "дни просрочки", "сумма задолженности"]
            },
            "фз 44-фз": {
                "synonyms": ["федеральный закон 44-фз", "закон о ключевой ставке", "лимит пени"],
                "norm_refs": ["ФЗ №44-ФЗ от 08.06.2020"],
                "contexts": ["ограничение ставки до 9.5%", "до 2027 года", "расчет пени по сниженной ставке"]
            },
            "постановление 329": {
                "synonyms": ["пп 329", "правила начисления пени", "новые правила пени"],
                "norm_refs": ["ПП РФ №329 от 06.05.2024"],
                "contexts": ["вступление в силу", "изменения в расчете", "переходный период"]
            },
            "приказное производство": {
                "synonyms": ["судебный приказ", "упрощённое взыскание", "без судебного заседания"],
                "norm_refs": ["ГПК РФ, ст. 122"],
                "contexts": ["сумма до 500 тыс. руб.", "возражения должника", "отмена приказа"]
            },
            "моральный вред": {
                "synonyms": ["компенсация морального вреда", "незаконные действия коллекторов", "угрозы", "давление"],
                "norm_refs": ["ГК РФ, ст. 151", "ФЗ №230-ФЗ"],
                "contexts": ["доказательства", "судебная практика", "жалоба в прокуратуру"]
            },
            "запрет на выезд": {
                "synonyms": ["ограничение выезда", "запрет за долги", "судебные приставы", "фссп"],
                "norm_refs": ["ФЗ №229-ФЗ, ст. 67"],
                "contexts": ["сумма долга от 10 000 руб.", "уведомление", "снятие запрета после оплаты"]
            },
            "завышенное начисление": {
                "synonyms": ["ошибка в начислении", "необоснованный долг", "перерасчет долга", "жалоба на начисление"],
                "norm_refs": ["ПП РФ №354, п. 35", "ЖК РФ, ст. 157"],
                "contexts": ["акт проверки", "расчёт по нормативу", "повышающий коэффициент", "судебный спор"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "fssp.gov.ru", "vsrf.ru", "ksrf.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".fssp.gov.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ЖК РФ ст 155.1")
        queries.append(f"{query} ПП РФ 329 пени")
        queries.append(f"{query} ФЗ 44-ФЗ ключевая ставка")
        queries.append(f"{query} судебная практика по долгам ЖКХ")
        queries.append(f"{query} ограничение выезда за долги ФССП")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Задолженности.
        Формирует системный промт для Saiga/LLaMA-3 8B:
        - Фокус: задолженность, пени, сроки оплаты, взыскание, судебная практика.
        - Жесткая структура.
        - Только официальные источники (ЖК РФ, ПП РФ, ФЗ, ГПК).
        Вопрос пользователя добавляется отдельно в generate_answer_chat.
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        penalty_keywords = [
            "пени", "пеня", "неустойка", "штраф за просрочку",
            "ставка цб", "ключевая ставка", "расчет пени",
            "проценты за просрочку", "начисление пени"
        ]
        q_lower = summary.lower()
        should_calculate_penalty = any(kw in q_lower for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — эксперт по задолженностям и взысканию долгов в сфере ЖКХ. "
            "Дай точный, юридически обоснованный и структурированный ответ, "
            "используя ТОЛЬКО контекст, результаты поиска и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если данных нет — отвечай: 'Недостаточно данных для точного ответа.'\n"
            "2. Все ссылки обязательны ([ЖК РФ, ст. 155.1], [ПП РФ №354, п. 118], [ГПК РФ, ст. 122]).\n"
            "3. Соблюдай структуру ответа.\n"
            "4. Формулы пени только если есть ключевые слова.\n"
            "5. Приоритет источников: ЖК РФ > ПП РФ > ФЗ > судебная практика.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "### Структура ответа:\n"
            "- Краткий вывод\n"
            "- Нормативное обоснование (точные статьи)\n"
            "- Расчет пени (если применимо)\n"
            "- Сроки оплаты и взыскания\n"
            "- Судебная практика\n"
        )
    
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчет пени (актуальная формула):**\n"
                "- Пени = Долг × Дни просрочки × (Ключевая ставка ЦБ / 300 / 100)\n"
                "- Основание: [ЖК РФ, ст. 155.1]\n"
                "- Лимит: ≤ 9.5% годовых [ФЗ №44-ФЗ, ПП РФ №329]\n"
                "- Начало: с 31-го дня после срока оплаты.\n"
            )
    
        system_prompt += (
            "\n### Ключевые нормативные акты (справочно):\n"
            "- ЖК РФ (ст. 155 — сроки оплаты, ст. 155.1 — пени, ст. 158 — взыскание)\n"
            "- ПП РФ №354 (раздел 8 — порядок расчетов)\n"
            "- ПП РФ №329 (порядок начисления пени с 2024 года)\n"
            "- ФЗ №44-ФЗ (ограничение ставки ЦБ до 9.5% до 2027 г.)\n"
            "- ФЗ №229-ФЗ (исполнительное производство)\n"
            "- ГПК РФ (приказное производство, взыскание через суд)\n\n"
            f"{self.get_role_instruction(role)}"
        )
    
        return (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )

class DisclosureAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Раскрытие информации", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "раскрытие": {
                "synonyms": ["публичное раскрытие", "открытость", "доступность информации", "прозрачность"],
                "norm_refs": ["ФЗ №209-ФЗ", "ПП РФ №731"],
                "contexts": ["обязанность УК", "стандарты", "формы отчетов"]
            },
            "гис жкх": {
                "synonyms": ["госуслуги жкх", "портал жкх", "гисжкх", "единая информационная система жкх"],
                "norm_refs": ["ПП РФ №731, п. 3", "Приказ Минстроя №74/пр"],
                "contexts": ["обязательное размещение", "сроки загрузки", "почему нет данных", "технические сбои"]
            },
            "отчёт": {
                "synonyms": ["финансовый отчет", "годовой отчет", "отчет об исполнении договора", "бюджет дома", "смета расходов"],
                "norm_refs": ["ПП РФ №731, Приложение 2", "Приказ Минстроя №48/414"],
                "contexts": ["структура отчета", "сроки публикации", "где посмотреть", "не публикуют"]
            },
            "информация": {
                "synonyms": ["данные", "документы", "доступ к документам", "копия договора", "протокол собрания"],
                "norm_refs": ["ПП РФ №731, Приложение 1", "ФЗ №59-ФЗ"],
                "contexts": ["запрос информации", "отказ в предоставлении", "сроки ответа", "жалоба"]
            },
            "доступ": {
                "synonyms": ["личный кабинет", "информационный стенд", "телеграм-канал", "сайт УК", "публичный доступ"],
                "norm_refs": ["ПП РФ №731, п. 3"],
                "contexts": ["обязательные каналы", "альтернативные способы", "технические требования"]
            },
            "протоколы собраний": {
                "synonyms": ["протокол ОСС", "решения собрания", "итоги голосования", "копия протокола"],
                "norm_refs": ["ЖК РФ, ст. 46", "ПП РФ №731, Приложение 1"],
                "contexts": ["сроки размещения", "обязательные реквизиты", "где хранятся", "как получить"]
            },
            "не публикуют отчеты": {
                "synonyms": ["нет информации в ГИС ЖКХ", "не отвечают на запросы", "отказ в предоставлении информации", "скрывают данные"],
                "norm_refs": ["ПП РФ №731, п. 10", "ФЗ №59-ФЗ, ст. 12"],
                "contexts": ["ответственность УК", "жалоба в ГЖИ", "штрафы", "судебная практика"]
            },
            "сроки загрузки": {
                "synonyms": ["сроки размещения", "когда публиковать", "когда размещать", "обновлять информацию", "сроки обновления"],
                "norm_refs": ["ПП РФ №731, п. 3(3)", "Приказ Минстроя №74/пр"],
                "contexts": ["ежемесячно", "ежеквартально", "ежегодно", "в течение 3 рабочих дней", "в течение 10 дней"]
            },
            "загружать": {
                "synonyms": ["грузить", "публиковать", "размещать", "загрузить", "опубликовать", "обновить"],
                "norm_refs": ["ПП РФ №731", "Приказ Минстроя №74/пр"],
                "contexts": ["процедура загрузки", "форматы файлов", "ответственный сотрудник", "подтверждение публикации"]
            },
            "почему в гис жкх нет данных": {
                "synonyms": ["когда появится в гис жкх", "техническая ошибка", "не синхронизировано", "не загружено"],
                "norm_refs": ["ПП РФ №731", "Приказ Минстроя №74/пр"],
                "contexts": ["сроки технической загрузки", "обращение в техподдержку", "жалоба на нарушение сроков"]
            },
            "план работ": {
                "synonyms": ["график работ", "перечень услуг", "годовой план", "план-график"],
                "norm_refs": ["ПП РФ №731, Приложение 1", "Приказ Минстроя №48/414"],
                "contexts": ["обязательность публикации", "согласование с собственниками", "изменения в плане"]
            },
            "копия договора": {
                "synonyms": ["договор управления", "текст договора", "условия договора", "скачать договор"],
                "norm_refs": ["ЖК РФ, ст. 162", "ПП РФ №731, Приложение 1"],
                "contexts": ["право на получение", "срок предоставления", "формат (PDF, бумажный)", "бесплатно"]
            },
            "отказ в предоставлении информации": {
                "synonyms": ["не отвечают", "игнорируют запрос", "не дают документы", "сокрытие информации"],
                "norm_refs": ["ФЗ №59-ФЗ, ст. 12", "ПП РФ №731, п. 10"],
                "contexts": ["досудебная претензия", "жалоба в прокуратуру", "исковое заявление", "моральный вред"]
            },
            "информационный стенд": {
                "synonyms": ["стенд в подъезде", "доска объявлений", "место для информации", "офлайн доступ"],
                "norm_refs": ["ПП РФ №731, п. 3(1)"],
                "contexts": ["обязательное размещение", "содержание стенда", "ответственность за актуальность"]
            },
            "личный кабинет": {
                "synonyms": ["кабинет жильца", "онлайн-сервис", "мобильное приложение", "портал УК"],
                "norm_refs": ["ПП РФ №731, п. 3(2)"],
                "contexts": ["необязательный, но рекомендуемый", "требования к функционалу", "безопасность данных"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "dom.gosuslugi.ru", "gkh354.ru", "gjirf.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".gosuslugi.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ПП РФ 731")
        queries.append(f"{query} ГИС ЖКХ сроки загрузки")
        queries.append(f"{query} Приказ Минстроя 48/414")
        queries.append(f"{query} ФЗ 209-ФЗ раскрытие информации")
        queries.append(f"{query} судебная практика по отказу в предоставлении информации ЖКХ")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Раскрытие информации.
        Формирует системный промт для Saiga/LLaMA-3 8B:
        - Фокус: раскрытие информации УК/РСО, ГИС ЖКХ, сроки, форматы, ответственность
        - Жесткая структура
        - Только официальные источники (ФЗ, ПП РФ, Приказы Минстроя)
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        penalty_keywords = [
            "пени", "пеня", "неустойка", "штраф за просрочку",
            "ставка цб", "ключевая ставка", "расчет пени"
        ]
        q_lower = summary.lower()
        should_calculate_penalty = any(kw in q_lower for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — эксперт по раскрытию информации в сфере ЖКХ. "
            "Дай точный, структурированный и юридически обоснованный ответ, "
            "используя ТОЛЬКО контекст, результаты поиска и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если данных нет — отвечай: 'Недостаточно данных для точного ответа.'\n"
            "2. Все утверждения обязательны со ссылками ([ПП РФ №731, п. 3], [ФЗ №209-ФЗ, ст. 161], [Приказ Минстроя №74/пр]).\n"
            "3. Ответ строго по структуре.\n"
            "4. Формулы пени только если есть ключевые слова.\n"
            "5. Приоритет источников: ФЗ > ПП РФ > Приказы Минстроя > разъяснения надзорных органов.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "### Структура ответа:\n"
            "- Краткий вывод\n"
            "- Нормативное обоснование (точные статьи и пункты)\n"
            "- Пошаговая инструкция (как раскрывать, где публиковать, кто отвечает)\n"
            "- Сроки и ответственность\n"
            "- Судебная практика\n"
        )
    
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчет пени (актуальная формула):**\n"
                "- Пени = Долг × Дни просрочки × (Ключевая ставка ЦБ / 300 / 100)\n"
                "- Основание: [ЖК РФ, ст. 155.1]\n"
                "- Ограничение: ≤ 9.5% годовых [ФЗ №44-ФЗ, ПП РФ №329]\n"
                "- Начало: с 31-го дня после срока оплаты.\n"
            )
    
        system_prompt += (
            "\n### Ключевые нормативные акты (справочно):\n"
            "- ФЗ №209-ФЗ (обязанность раскрытия информации)\n"
            "- ПП РФ №731 (стандарты раскрытия: структура, сроки, каналы)\n"
            "- Приказ Минстроя №48/пр, №414 (годовая отчетность УК)\n"
            "- Приказ Минстроя №74/пр (загрузка данных в ГИС ЖКХ)\n"
            "- ФЗ №59-ФЗ (сроки ответа на запросы граждан — 30 дней)\n\n"
            f"{self.get_role_instruction(role)}"
        )
    
        return (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )

class IoTAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("IoT и мониторинг", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "датчик": {
                "synonyms": ["сенсор", "iot-датчик", "умный датчик", "датчик протечки", "датчик задымления", "датчик температуры"],
                "norm_refs": ["ФЗ №152-ФЗ", "ПП РФ №689"],
                "contexts": ["установка", "интеграция", "оповещение", "аварийное отключение"]
            },
            "утечка": {
                "synonyms": ["протечка", "затопление", "авария водоснабжения", "аварийное оповещение"],
                "norm_refs": [],
                "contexts": ["датчик протечки", "автоматическое перекрытие", "уведомление в Telegram", "интеграция с УК"]
            },
            "температура": {
                "synonyms": ["умный термостат", "датчик температуры", "климат-контроль", "нагрев", "охлаждение"],
                "norm_refs": [],
                "contexts": ["регулирование отопления", "экономия энергии", "графики температуры", "интеграция с ИТП"]
            },
            "iot": {
                "synonyms": ["интернет вещей", "умные устройства", "smart devices", "цифровизация ЖКХ"],
                "norm_refs": ["ФЗ №149-ФЗ", "ФЗ №152-ФЗ"],
                "contexts": ["инфраструктура", "облачные платформы", "API", "вебхуки", "безопасность"]
            },
            "умный": {
                "synonyms": ["интеллектуальный", "автоматизированный", "connected", "smart home"],
                "norm_refs": [],
                "contexts": ["счётчик", "термостат", "замок", "система управления", "интеграция"]
            },
            "мониторинг": {
                "synonyms": ["наблюдение", "контроль", "телеметрия", "сбор данных", "аналитика"],
                "norm_refs": [],
                "contexts": ["в реальном времени", "графики потребления", "оповещения", "отчеты"]
            },
            "авария": {
                "synonyms": ["ЧП", "инцидент", "аварийная ситуация", "оповещение об аварии"],
                "norm_refs": [],
                "contexts": ["автоматическое оповещение", "реагирование УК", "интеграция с диспетчерской", "SMS/Telegram"]
            },
            "умный счетчик воды": {
                "synonyms": ["электронный счётчик воды", "телеметрический счётчик", "счётчик с GSM", "автоматическая передача"],
                "norm_refs": ["ФЗ №261-ФЗ, ст. 13(5)", "ПП РФ №354, п. 31"],
                "contexts": ["дистанционная передача", "интеграция с ГИС ЖКХ", "тарифы", "замена"]
            },
            "умный счетчик тепла": {
                "synonyms": ["теплосчётчик", "распределитель тепла", "радиаторный счётчик", "ИПУ тепла"],
                "norm_refs": ["ПП РФ №354, раздел 5", "ФЗ №261-ФЗ"],
                "contexts": ["расчёт по показаниям", "поверка", "передача данных", "интеграция с системой учёта"]
            },
            "интеграция с умным домом": {
                "synonyms": ["совместимость", "API", "вебхуки", "Yandex Smart Home", "Apple HomeKit", "Google Home"],
                "norm_refs": ["ФЗ №149-ФЗ", "ФЗ №152-ФЗ"],
                "contexts": ["безопасность", "авторизация", "передача данных", "локальные шлюзы"]
            },
            "уведомления в телеграм": {
                "synonyms": ["telegram-бот", "оповещения в whatsapp", "push-уведомления", "sms-оповещения", "email-рассылка"],
                "norm_refs": ["ФЗ №152-ФЗ, ст. 9", "ПП РФ №689"],
                "contexts": ["настройка", "согласие пользователя", "отказ от рассылки", "безопасность каналов"]
            },
            "api для интеграции": {
                "synonyms": ["вебхуки", "REST API", "интерфейс интеграции", "документация API", "SDK"],
                "norm_refs": ["ФЗ №149-ФЗ", "ФЗ №152-ФЗ"],
                "contexts": ["безопасность", "аутентификация", "rate limiting", "логирование", "передача персональных данных"]
            },
            "вебхуки": {
                "synonyms": ["webhook", "callback", "HTTP-уведомления", "асинхронные уведомления"],
                "norm_refs": ["ФЗ №149-ФЗ", "ФЗ №152-ФЗ"],
                "contexts": ["настройка", "безопасность (HTTPS, подписи)", "обработка ошибок", "повторные попытки"]
            },
            "безопасность данных": {
                "synonyms": ["защита информации", "шифрование", "GDPR", "персональные данные", "конфиденциальность"],
                "norm_refs": ["ФЗ №152-ФЗ", "ПП РФ №689", "ФЗ №149-ФЗ"],
                "contexts": ["хранение", "передача", "согласие", "аудит", "ответственность оператора"]
            },
            "перспективы развития": {
                "synonyms": ["будущее IoT", "цифровая трансформация ЖКХ", "искусственный интеллект", "предиктивная аналитика"],
                "norm_refs": [],
                "contexts": ["госпрограммы", "гранты", "пилотные проекты", "стандартизация"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "digital.gov.ru", "roskomnadzor.ru", "fct.gov.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".roskomnadzor.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ФЗ 152-ФЗ IoT")
        queries.append(f"{query} ПП РФ 689 персональные данные")
        queries.append(f"{query} умные счётчики ЖКХ")
        queries.append(f"{query} интеграция API датчиков ЖКХ")
        queries.append(f"{query} уведомления в Telegram датчики протечки")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: IoT и цифровой мониторинг.
        Формирует системный промт для Saiga/LLaMA-3 8B:
        - Фокус: IoT, цифровой мониторинг, интеграции, уведомления
        - Правовые аспекты: ФЗ-152, ПП РФ №689
        - Жёсткая структура и ссылки на нормативные акты
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        penalty_keywords = [
            "пени", "пеня", "неустойка", "штраф за просрочку",
            "ставка цб", "ключевая ставка", "расчет пени"
        ]
        should_calculate_penalty = any(kw in summary.lower() for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — эксперт по IoT и цифровому мониторингу в ЖКХ. "
            "Дай точный, структурированный и юридически обоснованный ответ, "
            "используя ТОЛЬКО контекст, веб-результаты и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если данных нет — отвечай: 'Недостаточно данных для точного ответа.'\n"
            "2. Все утверждения сопровождай ссылками на нормативные акты ([ФЗ №152-ФЗ, ст. 9], [ПП РФ №689, п. 4]).\n"
            "3. Структура ответа: Краткий вывод → Техническое решение → Нормативные требования → Рекомендации по внедрению.\n"
            "4. Формулы пени только при наличии ключевых слов.\n"
            "5. Приоритет источников: ФЗ > ПП РФ > технические стандарты.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "### Структура ответа:\n"
            "- Краткий вывод\n"
            "- Техническое решение / возможности:\n"
            "  * Устройства и технологии\n"
            "  * Интеграция (API, вебхуки, протоколы)\n"
            "  * Настройка уведомлений (Telegram, WhatsApp, SMS)\n"
            "- Нормативные требования:\n"
            "  * Законодательство по обработке данных [ФЗ №152-ФЗ]\n"
            "  * Согласие жильцов [ФЗ №152-ФЗ, ст. 9]\n"
            "  * Меры безопасности [ПП РФ №689]\n"
            "- Рекомендации по внедрению:\n"
            "  * Этапы подключения\n"
            "  * Избежание юридических рисков\n"
            "  * Примеры успешных кейсов (если есть в контексте)\n"
        )
    
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчет пени (актуальная формула):**\n"
                "- Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ / 300 / 100)\n"
                "- Основание: [ЖК РФ, ст. 155.1]\n"
                "- Ограничение: ≤ 9.5% годовых [ФЗ №44-ФЗ, ПП РФ №329]\n"
                "- Начало: с 31-го дня после срока оплаты.\n"
            )
    
        system_prompt += (
            "\n### Ключевые нормативные акты:\n"
            "- ФЗ №152-ФЗ «О персональных данных»\n"
            "- ПП РФ №689 «Об утверждении требований к защите персональных данных»\n"
            "- ФЗ №149-ФЗ «Об информации, ИТ и защите информации»\n"
            "- ФЗ №261-ФЗ (умные счетчики, IoT)\n"
            "- ПП РФ №354 (интеграция показаний счетчиков)\n\n"
            f"{self.get_role_instruction(role)}"
        )
    
        return (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )

        
class MeetingAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Общие собрания", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "собрание": {
                "synonyms": ["осс", "общее собрание собственников", "внеочередное собрание", "очное собрание", "заочное собрание"],
                "norm_refs": ["ЖК РФ, ст. 44-48", "ПП РФ №416"],
                "contexts": ["инициатор", "повестка", "уведомление", "проведение", "недействительность"]
            },
            "голосование": {
                "synonyms": ["электронное голосование", "голосование через ГИС ЖКХ", "заочное голосование", "онлайн-голосование"],
                "norm_refs": ["ЖК РФ, ст. 47", "ПП РФ №416, п. 12"],
                "contexts": ["кворум", "форма бюллетеня", "сроки", "результаты", "подтверждение голоса"]
            },
            "решение": {
                "synonyms": ["итоги голосования", "принятое решение", "резолюция", "постановление собрания"],
                "norm_refs": ["ЖК РФ, ст. 46", "ПП РФ №416, п. 21"],
                "contexts": ["обязательность для всех", "оспаривание", "исполнение", "жалоба"]
            },
            "протокол": {
                "synonyms": ["акт собрания", "итоговый документ", "протокол ОСС", "подписать протокол"],
                "norm_refs": ["ЖК РФ, ст. 46(5)", "ПП РФ №416, п. 21"],
                "contexts": ["обязательные реквизиты", "сроки составления", "хранение", "публикация в ГИС ЖКХ"]
            },
            "кворум": {
                "synonyms": ["кворум собрания", "количество голосов", "порог принятия решений", "большинство голосов"],
                "norm_refs": ["ЖК РФ, ст. 46(1)", "ПП РФ №416, п. 18"],
                "contexts": ["2/3", "50%+1", "в зависимости от вопроса", "расчёт по долям"]
            },
            "инициатор собрания": {
                "synonyms": ["кто может созвать", "организатор собрания", "инициативная группа", "совет дома", "ТСЖ"],
                "norm_refs": ["ЖК РФ, ст. 45(1)", "ПП РФ №416, п. 3"],
                "contexts": ["право инициировать", "обязанность УК", "уведомление", "форма заявления"]
            },
            "уведомление собственников": {
                "synonyms": ["форма уведомления", "способ оповещения", "почтовое уведомление", "размещение в ГИС ЖКХ"],
                "norm_refs": ["ЖК РФ, ст. 45(3)", "ПП РФ №416, п. 5"],
                "contexts": ["сроки (не позднее 10 дней)", "обязательные сведения", "электронные каналы", "подтверждение вручения"]
            },
            "повестка": {
                "synonyms": ["вопросы собрания", "агенда", "перечень вопросов", "темы для голосования"],
                "norm_refs": ["ЖК РФ, ст. 45(4)", "ПП РФ №416, п. 6"],
                "contexts": ["обязательные и дополнительные вопросы", "изменение повестки", "внеочередные вопросы"]
            },
            "недействительное собрание": {
                "synonyms": ["нарушение процедуры", "оспаривание решения", "жалоба на решение", "признание недействительным"],
                "norm_refs": ["ЖК РФ, ст. 46(5)", "ПП РФ №416, п. 25"],
                "contexts": ["сроки оспаривания (6 месяцев)", "основания", "судебная практика", "доказательства нарушений"]
            },
            "совет дома": {
                "synonyms": ["председатель совета", "инициативная группа", "орган управления МКД", "представитель собственников"],
                "norm_refs": ["ЖК РФ, ст. 161.1", "ПП РФ №416, п. 3(2)"],
                "contexts": ["право созыва собрания", "подготовка материалов", "ведение протокола", "взаимодействие с УК"]
            },
            "тсж": {
                "synonyms": ["товарищество собственников жилья", "кооператив", "организация управления"],
                "norm_refs": ["ЖК РФ, ст. 135-154"],
                "contexts": ["право созыва собрания", "полномочия", "отчётность", "реорганизация"]
            },
            "повторное собрание": {
                "synonyms": ["второе собрание", "резервное собрание", "собрание при отсутствии кворума"],
                "norm_refs": ["ЖК РФ, ст. 47(4)", "ПП РФ №416, п. 19"],
                "contexts": ["сроки проведения (не позднее 20 дней)", "уменьшенный кворум", "особенности повестки"]
            },
            "акт приёмки": {
                "synonyms": ["подписать акт", "приёмка работ", "сдача объекта", "ввод в эксплуатацию", "организация подписания"],
                "norm_refs": ["ЖК РФ, ст. 44(2)", "ПП РФ №416, п. 21(3)"],
                "contexts": ["включение в повестку", "голосование за приёмку", "ответственность за отказ подписания", "связь с капремонтом"]
            },
            "электронное голосование": {
                "synonyms": ["голосование через ГИС ЖКХ", "онлайн голосование", "электронный бюллетень", "голосование через портал госуслуг"],
                "norm_refs": ["ЖК РФ, ст. 47(3)", "ПП РФ №416, п. 12", "Приказ Минстроя №74/пр"],
                "contexts": ["требования к системе", "идентификация", "сроки", "равнозначность очному голосованию"]
            },
            "жалоба на решение": {
                "synonyms": ["оспаривание протокола", "обжалование ОСС", "заявление в суд", "досудебная претензия"],
                "norm_refs": ["ЖК РФ, ст. 46(5)", "ГПК РФ, ст. 131"],
                "contexts": ["срок 6 месяцев", "доказательства нарушений", "роль ГЖИ", "судебные издержки"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "dom.gosuslugi.ru", "gjirf.ru", "vsrf.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".gosuslugi.ru", ".vsrf.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ЖК РФ ст 44-48")
        queries.append(f"{query} ПП РФ 416")
        queries.append(f"{query} электронное голосование ГИС ЖКХ")
        queries.append(f"{query} оспаривание решения ОСС судебная практика")
        queries.append(f"{query} протокол общего собрания форма")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Общие собрания собственников.
        Формирует системный промт для Saiga/LLaMA-3 8B:
        - Фокус: инициирование, уведомление, кворум, голосование, протокол, оспаривание решений
        - Жёсткая структура и ссылки на нормативные акты
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        penalty_keywords = [
            "пени", "пеня", "неустойка", "штраф за просрочку",
            "ставка цб", "ключевая ставка", "расчет пени"
        ]
        should_calculate_penalty = any(kw in summary.lower() for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — эксперт по проведению общих собраний собственников в многоквартирных домах. "
            "Дай точный, структурированный и юридически обоснованный ответ, используя ТОЛЬКО контекст, веб-результаты и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если данных нет — отвечай: 'Недостаточно данных для точного ответа.'\n"
            "2. Все утверждения подкрепляй ссылками на нормативные акты ([ЖК РФ, ст. 45], [ПП РФ №416, п. 5]).\n"
            "3. Структура ответа: Краткий вывод → Нормативное обоснование → Пошаговая инструкция → Судебная практика.\n"
            "4. Формулы пени только при наличии ключевых слов.\n"
            "5. Приоритет источников: ЖК РФ > ПП РФ > разъяснения Минстроя/ГЖИ > судебная практика.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "### Структура ответа:\n"
            "- Краткий вывод\n"
            "- Нормативное обоснование (статьи ЖК РФ, пункты ПП РФ)\n"
            "- Пошаговая инструкция:\n"
            "  * Кто может инициировать собрание? (ЖК РФ, ст. 45)\n"
            "  * Уведомление собственников (сроки, форма, способы — ПП РФ №416, п. 5)\n"
            "  * Расчет кворума и проведение голосования (ЖК РФ, ст. 46-47)\n"
            "  * Оформление и публикация протокола (ПП РФ №416, п. 21)\n"
            "  * Оспаривание решений (сроки, основания, порядок — ЖК РФ, ст. 46(5))\n"
            "- Судебная практика\n"
        )
    
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчет пени (актуальная формула):**\n"
                "- Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Нормативная база: [ЖК РФ, ст. 155.1]\n"
                "- Ограничение: ≤ 9.5% годовых [ФЗ №44-ФЗ, ПП РФ №329]\n"
                "- Начало: с 31-го дня после срока оплаты.\n"
            )
    
        system_prompt += (
            "\n### Ключевые нормативные акты:\n"
            "- ЖК РФ (ст. 44-48 — основы проведения ОСС)\n"
            "- ПП РФ №416 «О порядке проведения общего собрания»\n"
            "- Приказ Минстроя №74/пр (технические требования к электронному голосованию)\n"
            "- ГПК РФ (порядок оспаривания решений в суде)\n\n"
            f"{self.get_role_instruction(role)}"
        )
    
        return (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )
        
class CapitalRepairAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Капитальный ремонт", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "капремонт": {
                "synonyms": ["капитальный ремонт", "откапиталить", "ремонт дома", "капитальный ремонт МКД"],
                "norm_refs": ["ЖК РФ, ст. 166-180", "ПП РФ №416"],
                "contexts": ["программа", "сроки", "перенос", "финансирование", "приемка"]
            },
            "спецсчет": {
                "synonyms": ["специальный счет", "спецсчёт", "счёт для капремонта", "номер счёта"],
                "norm_refs": ["ЖК РФ, ст. 170", "ПП РФ №416, п. 12"],
                "contexts": ["открытие", "управление", "отчётность", "смена оператора", "региональный оператор"]
            },
            "фонд": {
                "synonyms": ["фонд капремонта", "взносы на капремонт", "накопления", "средства собственников"],
                "norm_refs": ["ЖК РФ, ст. 169", "ПП РФ №416, п. 8"],
                "contexts": ["формирование", "расходование", "отчётность", "проверка", "возврат средств"]
            },
            "программа капремонта": {
                "synonyms": ["региональная программа", "график ремонта", "перечень домов", "сроки ремонта"],
                "norm_refs": ["ЖК РФ, ст. 168", "ПП РФ №416, п. 5"],
                "contexts": ["утверждение", "корректировка", "перенос сроков", "изменение перечня работ"]
            },
            "инженерные сети": {
                "synonyms": ["электрика", "электропроводка", "труба", "отопление", "ГВС", "ХВС", "вентиляция", "пожарная сигнализация"],
                "norm_refs": ["ЖК РФ, ст. 166(1)", "ПП РФ №416, Приложение 1"],
                "contexts": ["что входит в ремонт", "замена", "модернизация", "приёмка", "гарантия"]
            },
            "фасад": {
                "synonyms": ["внешний вид дома", "восстановить фасад", "очистить фасад", "граффити", "вандализм", "реклама на фасаде"],
                "norm_refs": ["ЖК РФ, ст. 166(1)", "ПП РФ №416, Приложение 1"],
                "contexts": ["ремонт штукатурки", "покраска", "восстановление после вандализма", "согласование с администрацией"]
            },
            "лифт": {
                "synonyms": ["замена лифта", "модернизация лифта", "капитальный ремонт лифта", "ввод в эксплуатацию лифта"],
                "norm_refs": ["ЖК РФ, ст. 166(1)", "ПП РФ №416, Приложение 1", "ТР ТС 011/2011"],
                "contexts": ["срок службы", "технические требования", "приёмка", "акт ввода в эксплуатацию"]
            },
            "подвал": {
                "synonyms": ["цокольный этаж", "техническое помещение", "ремонт подвала", "гидроизоляция подвала"],
                "norm_refs": ["ЖК РФ, ст. 166(1)", "ПП РФ №416, Приложение 1"],
                "contexts": ["гидроизоляция", "вентиляция", "электрощиты", "доступ для жильцов"]
            },
            "подрядчик капремонта": {
                "synonyms": ["исполнитель работ", "строительная компания", "подрядная организация", "заказчик работ"],
                "norm_refs": ["ЖК РФ, ст. 175", "ПП РФ №416, п. 15"],
                "contexts": ["выбор через ОСС", "тендер", "договор", "приёмка", "ответственность за качество"]
            },
            "приемка работ": {
                "synonyms": ["акт приемки капремонта", "подписать акт", "сдача объекта", "ввод в эксплуатацию", "комиссия по приёмке"],
                "norm_refs": ["ЖК РФ, ст. 176", "ПП РФ №416, п. 20"],
                "contexts": ["состав комиссии", "обязательные члены", "сроки", "отказ в подписании", "дефекты"]
            },
            "отчет о расходовании средств": {
                "synonyms": ["смета капремонта", "финансовый отчет", "бюджет ремонта", "расходование фонда"],
                "norm_refs": ["ЖК РФ, ст. 177", "ПП РФ №416, п. 22"],
                "contexts": ["публичность", "размещение в ГИС ЖКХ", "сроки публикации", "право на запрос информации"]
            },
            "перенос сроков капремонта": {
                "synonyms": ["поменяли сроки", "изменение программы", "отсрочка ремонта", "корректировка графика"],
                "norm_refs": ["ЖК РФ, ст. 168(4)", "ПП РФ №416, п. 7"],
                "contexts": ["основания", "решение ОСС", "согласование с региональным оператором", "жалоба"]
            },
            "региональный оператор": {
                "synonyms": ["фонд капремонта региона", "оператор капремонта", "регоператор", "государственный оператор"],
                "norm_refs": ["ЖК РФ, ст. 178", "ПП РФ №416, п. 10"],
                "contexts": ["обязанности", "отчётность", "переход на спецсчёт", "ответственность за неисполнение"]
            },
            "старший по дому": {
                "synonyms": ["председатель совета дома", "инициативная группа", "представитель собственников", "контактное лицо"],
                "norm_refs": ["ЖК РФ, ст. 161.1", "ПП РФ №416, п. 15(3)"],
                "contexts": ["права при приёмке", "участие в комиссии", "информирование жильцов", "взаимодействие с подрядчиком"]
            },
            "платить за капремонт": {
                "synonyms": ["обязанность платить", "взносы", "не платить за капремонт", "льготы по капремонту", "рассрочка"],
                "norm_refs": ["ЖК РФ, ст. 154(2)", "ЖК РФ, ст. 169", "ФЗ №271-ФЗ"],
                "contexts": ["обязательность", "расчёт суммы", "льготы", "пени за неуплату", "списание задолженности"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "reformagkh.ru", "kapremont.rf", "dom.gosuslugi.ru", "vsrf.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".kapremont.rf", ".vsrf.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ЖК РФ ст 166-180")
        queries.append(f"{query} ПП РФ 416 капремонт")
        queries.append(f"{query} региональная программа капитального ремонта")
        queries.append(f"{query} судебная практика по капремонту")
        queries.append(f"{query} спецсчет или региональный оператор")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Капитальный ремонт МКД
        Формирует системный промт для Saiga/LLaMA-3 8B:
        - Фокус: работы, фонд, подрядчик, приём, отчётность, региональные программы
        - Жёсткая структура и ссылки на нормативные акты
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        penalty_keywords = [
            "пени", "пеня", "неустойка", "штраф за просрочку",
            "ставка цб", "ключевая ставка", "расчет пени"
        ]
        should_calculate_penalty = any(kw in summary.lower() for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — эксперт по капитальному ремонту многоквартирных домов. "
            "Дай точный, структурированный и юридически корректный ответ, используя ТОЛЬКО контекст, веб-результаты и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если данных нет — отвечай: 'Недостаточно данных для точного ответа.'\n"
            "2. Все утверждения подкрепляй ссылками на нормативные акты ([ЖК РФ, ст. 166], [ПП РФ №416, п. 7]).\n"
            "3. Структура ответа: Краткий вывод → Нормативное обоснование → Пошаговая инструкция → Судебная практика.\n"
            "4. Формулы пени только при наличии ключевых слов.\n"
            "5. Приоритет источников: ЖК РФ > ПП РФ > региональные акты > разъяснения Минстроя > судебная практика.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "### Структура ответа:\n"
            "- Краткий вывод\n"
            "- Нормативное обоснование (статьи ЖК РФ, пункты ПП РФ, региональные акты)\n"
            "- Пошаговая инструкция:\n"
            "  * Что входит в капремонт? (ЖК РФ, ст. 166)\n"
            "  * Как формируется фонд? (ЖК РФ, ст. 170)\n"
            "  * Как выбрать подрядчика? (ОСС, ЖК РФ, ст. 175)\n"
            "  * Как принять работы? (состав комиссии, акт — ЖК РФ, ст. 176)\n"
            "  * Как получить отчёт? (сроки, форма, публикация — ЖК РФ, ст. 177)\n"
            "  * Что делать при переносе сроков или вандализме? (жалоба, повторное голосование)\n"
            "- Судебная практика\n"
        )
    
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчет пени (актуальная формула):**\n"
                "- Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Нормативная база: [ЖК РФ, ст. 155.1]\n"
                "- Ограничение: ≤ 9.5% годовых [ФЗ №44-ФЗ, ПП РФ №329]\n"
                "- Начало: с 31-го дня после срока оплаты.\n"
            )
    
        system_prompt += (
            "\n### Ключевые нормативные акты:\n"
            "- ЖК РФ (ст. 166-180 — основы капремонта)\n"
            "- ПП РФ №416 «О порядке проведения капитального ремонта»\n"
            "- ФЗ №271-ФЗ «О внесении изменений в ЖК РФ (по капремонту)»\n"
            "- Региональная программа капитального ремонта (если вопрос региональный)\n"
            "- Приказы Минстроя по формам отчётов и актов\n\n"
            f"{self.get_role_instruction(role)}"
        )
    
        return (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )

class EmergencyAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Аварии и инциденты", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "авария": {
                "synonyms": ["чп", "инцидент", "прорыв", "отключение", "поломка", "аварийная ситуация"],
                "norm_refs": ["ПП РФ №354, п. 98", "Правила технической эксплуатации ЖКХ"],
                "contexts": ["вода", "отопление", "электричество", "канализация", "сроки устранения"]
            },
            "затопило": {
                "synonyms": ["залило", "протечка", "потоп", "течь", "затопили соседи", "залило сверху"],
                "norm_refs": ["ПП РФ №354, п. 99", "ЖК РФ, ст. 161"],
                "contexts": ["акт о заливе", "фото", "оценка ущерба", "регресс к УК", "возмещение"]
            },
            "нет воды": {
                "synonyms": ["без воды", "перебои", "постоянные перебои", "нет горячей воды", "нет холодной воды"],
                "norm_refs": ["ПП РФ №354, п. 98(1)", "СанПиН 1.2.3685-21, п. 9.4"],
                "contexts": ["авария на магистрали", "ремонт стояка", "плановое отключение", "перерасчет"]
            },
            "отопление": {
                "synonyms": ["холодно в квартире", "не греет", "батарея холодная", "радиатор не работает", "отсутствие тепла"],
                "norm_refs": ["ПП РФ №354, п. 54(2)", "СанПиН 1.2.3685-21, п. 9.2"],
                "contexts": ["замер температуры", "авария на ЦТП", "воздух в системе", "сроки устранения"]
            },
            "канализация": {
                "synonyms": ["запах канализации", "подвал затоплен", "течь канализации", "откачка", "слив", "стоки"],
                "norm_refs": ["ПП РФ №354, п. 98(3)", "Правила технической эксплуатации ЖКХ"],
                "contexts": ["авария на стояке", "засор", "санитарная угроза", "срочный вызов"]
            },
            "угроза обрушения": {
                "synonyms": ["обрушение", "трещина", "шов", "стена", "фасад", "камень падает", "грозит обвалом"],
                "norm_refs": ["Правила технической эксплуатации ЖКХ", "ПП РФ №491, п. 10"],
                "contexts": ["немедленный вызов", "эвакуация", "акт обследования", "приостановка проживания"]
            },
            "вызвать аварийку": {
                "synonyms": ["куда звонить", "аварийная служба", "диспетчер", "телефон аварийной службы", "аварийка"],
                "norm_refs": ["ПП РФ №416, п. 3", "ПП РФ №354, п. 98"],
                "contexts": ["круглосуточный номер", "единая диспетчерская", "мобильное приложение", "время реакции"]
            },
            "акт о заливе": {
                "synonyms": ["акт затопления", "акт протечки", "комиссия по заливу", "фотоотчёт", "фиксация ущерба"],
                "norm_refs": ["ПП РФ №354, п. 99", "ЖК РФ, ст. 161"],
                "contexts": ["состав комиссии", "сроки составления (1 день)", "обязательные реквизиты", "подписание"]
            },
            "возмещение ущерба": {
                "synonyms": ["требую компенсации", "подать в суд за залив", "оценка ущерба", "независимая экспертиза", "испортилась мебель"],
                "norm_refs": ["ЖК РФ, ст. 161", "ГК РФ, ст. 1064", "ПП РФ №354, п. 99"],
                "contexts": ["регресс к УК", "досудебная претензия", "судебный иск", "моральный вред"]
            },
            "магистральная труба": {
                "synonyms": ["магистральный стояк", "внутридомовая сеть", "ввод в квартиру", "теплотрасса", "бойлер"],
                "norm_refs": ["ПП РФ №491, п. 3", "Правила технической эксплуатации ЖКХ"],
                "contexts": ["ответственность УК", "замена за счёт фонда капремонта", "авария на магистрали"]
            },
            "параметры": {
                "synonyms": ["температура воды", "нормативные параметры", "давление", "напор", "качество услуги"],
                "norm_refs": ["СанПиН 1.2.3685-21, п. 9.3-9.4", "ПП РФ №354, п. 54"],
                "contexts": ["замер", "жалоба", "перерасчет", "некачественная услуга"]
            },
            "телефонограмма": {
                "synonyms": ["заявка", "обращение", "регистрация вызова", "номер заявки", "обратная связь"],
                "norm_refs": ["ПП РФ №354, п. 98(4)", "ПП РФ №416, п. 5"],
                "contexts": ["обязательная регистрация", "сроки исполнения", "отказ в принятии", "жалоба"]
            },
            "плесень после залива": {
                "synonyms": ["отошли обои", "грибок", "сырость", "санитарная угроза", "восстановление отделки"],
                "norm_refs": ["СанПиН 1.2.3685-21, п. 9.1", "ЖК РФ, ст. 161"],
                "contexts": ["причина — залив", "требование к УК об устранении", "экспертиза", "компенсация"]
            },
            "короткое замыкание": {
                "synonyms": ["нет света", "отключение электричества", "искра", "запах гари", "опасность возгорания"],
                "norm_refs": ["Правила технической эксплуатации ЖКХ", "ПП РФ №354, п. 98(2)"],
                "contexts": ["вызов электрика", "эвакуация", "проверка проводки", "ответственность УК"]
            },
            "пожар": {
                "synonyms": ["возгорание", "огонь", "дым", "эвакуация", "МЧС", "пожарная сигнализация"],
                "norm_refs": ["ФЗ №69-ФЗ", "Правила противопожарного режима"],
                "contexts": ["вызов 101", "действия УК", "проверка систем", "расследование"]
            },
            "комиссар": {
                "synonyms": ["представитель УК", "член комиссии", "инспектор", "техник", "диспетчер на выезде"],
                "norm_refs": ["ПП РФ №354, п. 99", "Правила технической эксплуатации ЖКХ"],
                "contexts": ["обязан прибыть", "составить акт", "зафиксировать повреждения", "дать рекомендации"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "mchs.gov.ru", "rospotrebnadzor.ru", "vsrf.ru", "gjirf.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".mchs.gov.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ПП РФ 354 аварии")
        queries.append(f"{query} ПП РФ 416 аварийная служба")
        queries.append(f"{query} акт о заливе ЖКХ")
        queries.append(f"{query} сроки устранения аварии отопление")
        queries.append(f"{query} судебная практика по возмещению ущерба за залив")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Аварийные ситуации ЖКХ
        Формирует системный промт для Saiga/LLaMA-3 8B:
        - Фокус: отключение воды/отопления, протечки, сроки реагирования, акты, перерасчёт, возмещение ущерба
        - Жёсткая структура и ссылки на нормативные акты
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        penalty_keywords = [
            "пени", "пеня", "неустойка", "штраф за просрочку",
            "ставка цб", "ключевая ставка", "расчет пени"
        ]
        should_calculate_penalty = any(kw in summary.lower() for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — эксперт по аварийным ситуациям в ЖКХ. "
            "Дай точный, структурированный и юридически корректный ответ, используя ТОЛЬКО контекст, веб-результаты и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если данных нет — отвечай: 'Недостаточно данных для точного ответа.'\n"
            "2. Подкрепляй все утверждения ссылками на нормативные акты ([ПП РФ №354, п. 98], [ЖК РФ, ст. 157]).\n"
            "3. Структура ответа: Краткий вывод → Нормативное обоснование → Пошаговая инструкция → Судебная практика.\n"
            "4. Формулы пени только при наличии ключевых слов.\n"
            "5. Приоритет источников: ПП РФ > ЖК РФ > СанПиН > Правила техэксплуатации > судебная практика.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "### Структура ответа:\n"
            "- Краткий вывод (1-2 предложения: что делать немедленно)\n"
            "- Нормативное обоснование (пункты ПП РФ, ЖК РФ, СанПиН)\n"
            "- Пошаговая инструкция:\n"
            "  * Куда звонить и как оформить заявку? (ПП РФ №416, п. 3)\n"
            "  * Сроки устранения (отопление — 1 сутки, вода — 4 часа — ПП РФ №354, п. 98)\n"
            "  * Как зафиксировать факт аварии (фото, акт, свидетели — ПП РФ №354, п. 99)\n"
            "  * Как получить перерасчет или возместить ущерб (ЖК РФ, ст. 157, ГК РФ, ст. 1064)\n"
            "- Судебная практика\n"
        )
    
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчет пени (актуальная формула):**\n"
                "- Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Нормативная база: [ЖК РФ, ст. 155.1]\n"
                "- Ограничение: ≤ 9.5% годовых [ФЗ №44-ФЗ, ПП РФ №329]\n"
                "- Начало: с 31-го дня после срока оплаты.\n"
            )
    
        system_prompt += (
            "\n### Ключевые нормативные акты:\n"
            "- ПП РФ №354 (п. 98-99 — аварии, сроки, акты)\n"
            "- ПП РФ №416 (обязанности аварийных служб)\n"
            "- ЖК РФ (ст. 157 — перерасчет, ст. 161 — ответственность УК)\n"
            "- СанПиН 1.2.3685-21 (параметры качества воды, воздуха, шума)\n"
            "- Правила технической эксплуатации жилищного фонда (Минстрой РФ)\n"
            "- ГК РФ (ст. 1064 — возмещение вреда)\n\n"
            f"{self.get_role_instruction(role)}"
        )
    
        return (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )

class ContractorAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Подрядчики и мастера", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "подрядчик": {
                "synonyms": ["исполнитель", "строительная компания", "бригада", "мастер", "сантехник", "электрик"],
                "norm_refs": ["ГК РФ, гл. 37", "ЖК РФ, ст. 162"],
                "contexts": ["выбор", "договор", "контроль", "жалоба", "расторжение", "гарантия"]
            },
            "вызов мастера": {
                "synonyms": ["направить сантехника", "вызвать электрика", "отправьте бригаду", "срочный вызов", "вызовите"],
                "norm_refs": ["ПП РФ №354, п. 98", "ЖК РФ, ст. 161"],
                "contexts": ["сроки реагирования", "регистрация заявки", "телефонограмма", "отказ в вызове"]
            },
            "договор": {
                "synonyms": ["договор подряда", "контракт", "соглашение", "договор с подрядчиком", "условия договора"],
                "norm_refs": ["ГК РФ, ст. 702-729", "ЖК РФ, ст. 162"],
                "contexts": ["существенные условия", "срок исполнения", "цена", "ответственность", "расторжение"]
            },
            "некачественный ремонт": {
                "synonyms": ["халатность мастера", "не устранили проблему", "переделайте работу", "брак", "дефект"],
                "norm_refs": ["ГК РФ, ст. 723", "ЖК РФ, ст. 162"],
                "contexts": ["акт скрытых работ", "претензия", "экспертиза", "взыскание убытков", "повторный ремонт"]
            },
            "акт приемки": {
                "synonyms": ["акт скрытых работ", "приемка-передача", "подписать акт", "не подписан", "замечания"],
                "norm_refs": ["ГК РФ, ст. 753", "ПП РФ №416, п. 20"],
                "contexts": ["обязательные реквизиты", "срок подписания", "односторонний акт", "отказ в подписании"]
            },
            "гарантийный срок": {
                "synonyms": ["гарантия", "срок гарантии", "претензия подрядчику", "требую устранить", "повторный вызов"],
                "norm_refs": ["ГК РФ, ст. 724", "ПП РФ №416, п. 21"],
                "contexts": ["продолжительность", "начало течения", "претензионный порядок", "взыскание через суд"]
            },
            "жалоба на подрядчика": {
                "synonyms": ["не решена проблема", "обращались", "игнорируют", "некомпетентный мастер", "требую замены"],
                "norm_refs": ["ЖК РФ, ст. 161", "ГК РФ, ст. 723"],
                "contexts": ["досудебная претензия", "жалоба в УК/ТСЖ", "обращение в ГЖИ", "судебный иск"]
            },
            "график работ": {
                "synonyms": ["план работ", "расписание", "когда приедут", "срок выполнения", "работы во дворе"],
                "norm_refs": ["ГК РФ, ст. 708", "ЖК РФ, ст. 162"],
                "contexts": ["согласование с жильцами", "информирование", "изменение графика", "срыв сроков"]
            },
            "фасад дома": {
                "synonyms": ["восстановить фасад", "очистить фасад", "граффити", "вандализм", "реклама на фасаде", "надпись на фасаде"],
                "norm_refs": ["ЖК РФ, ст. 36", "ПП РФ №491, п. 3"],
                "contexts": ["согласование установки", "самовольная установка", "обязанность УК по восстановлению", "штрафы"]
            },
            "коммунальные услуги": {
                "synonyms": ["нет горячей воды", "отключение", "температура", "ребенок", "пожилой", "соцзащита"],
                "norm_refs": ["ПП РФ №354, п. 98", "СанПиН 1.2.3685-21"],
                "contexts": ["срочный вызов", "льготные категории", "перерасчет", "моральный вред"]
            },
            "санитарные работы": {
                "synonyms": ["дератизация", "кошение", "мытье окон", "лавочки", "урны", "вывоз шин", "покрышки"],
                "norm_refs": ["Правила технической эксплуатации ЖКХ", "ПП РФ №491"],
                "contexts": ["периодичность", "ответственность подрядчика", "жалобы на качество", "фотоотчёт"]
            },
            "технические работы": {
                "synonyms": ["замена радиатора", "прочистка канализации", "ремонт кровли", "замена электропроводки", "труба забита"],
                "norm_refs": ["ГК РФ, гл. 37", "Правила технической эксплуатации ЖКХ"],
                "contexts": ["лицензия", "СРО", "акт выполненных работ", "приёмка", "гарантия"]
            },
            "самовольная установка": {
                "synonyms": ["кондиционер", "спутниковая антенна", "видеокамера", "без согласования", "на фасаде"],
                "norm_refs": ["ЖК РФ, ст. 36", "ПП РФ №491, п. 3(4)"],
                "contexts": ["обязанность согласования", "демонтаж за счёт нарушителя", "штраф", "судебный запрет"]
            },
            "фото и доказательства": {
                "synonyms": ["фото", "вложении", "видео", "документы", "доказательства", "акт с фото"],
                "norm_refs": ["ГК РФ, ст. 753", "ГПК РФ, ст. 67"],
                "contexts": ["обязательность приёма", "фиксация дефектов", "доказательная база в суде", "электронные документы"]
            },
            "адрес и локация": {
                "synonyms": ["подъезд", "дом", "адрес", "стена", "шов", "место работ"],
                "norm_refs": ["ЖК РФ, ст. 162", "ГК РФ, ст. 708"],
                "contexts": ["точное указание в договоре", "акт выполненных работ", "жалоба с координатами", "геопривязка"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "gjirf.ru", "vsrf.ru", "sro.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".vsrf.ru", ".sro.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ГК РФ глава 37 подряд")
        queries.append(f"{query} ЖК РФ ст 162 договор управления")
        queries.append(f"{query} ПП РФ 416 приемка работ")
        queries.append(f"{query} судебная практика по некачественному ремонту подрядчиком")
        queries.append(f"{query} гарантийный срок ремонт фасада")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Работа с подрядчиками и мастерами ЖКХ
        Формирует системный промт для Saiga/LLaMA-3 8B:
        - Фокус: вызов, фиксация работ, претензии, акты, взыскание убытков
        - Жёсткая структура и ссылки на нормативные акты
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        penalty_keywords = [
            "пени", "пеня", "неустойка", "штраф за просрочку",
            "ставка цб", "ключевая ставка", "расчет пени"
        ]
        should_calculate_penalty = any(kw in summary.lower() for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — эксперт по работе с подрядчиками и мастерами в ЖКХ. "
            "Дай точный, структурированный и юридически корректный ответ, используя ТОЛЬКО контекст, веб-результаты и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если данных нет — отвечай: 'Недостаточно данных для точного ответа.'\n"
            "2. Подкрепляй все утверждения ссылками на нормативные акты ([ГК РФ, ст. 723], [ПП РФ №416, п. 7]).\n"
            "3. Структура ответа: Краткий вывод → Нормативное обоснование → Пошаговая инструкция → Судебная практика.\n"
            "4. Формулы пени только при наличии ключевых слов.\n"
            "5. Приоритет источников: ГК РФ > ЖК РФ > ПП РФ > Правила техэксплуатации > судебная практика.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "### Структура ответа:\n"
            "- Краткий вывод (1-2 предложения: что делать немедленно или по закону)\n"
            "- Нормативное обоснование (статьи ГК РФ, ЖК РФ, ПП РФ)\n"
            "- Пошаговая инструкция:\n"
            "  * Как оформить вызов или заявку? (ЖК РФ, ст. 161)\n"
            "  * Как зафиксировать некачественную работу (фото, акт, свидетели — ГК РФ, ст. 753)\n"
            "  * Как направить претензию подрядчику (сроки, форма — ГК РФ, ст. 723)\n"
            "  * Как действовать при отказе подписать акт (односторонний акт — ПП РФ №416)\n"
            "  * Как взыскать убытки или добиться переделки (суд, экспертиза — ГК РФ, ст. 723, 724)\n"
            "- Судебная практика\n"
        )
    
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчет пени (актуальная формула):**\n"
                "- Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Нормативная база: [ЖК РФ, ст. 155.1]\n"
                "- Ограничение: ≤ 9.5% годовых [ФЗ №44-ФЗ, ПП РФ №329]\n"
                "- Начало: с 31-го дня после срока оплаты.\n"
            )
    
        system_prompt += (
            "\n### Ключевые нормативные акты:\n"
            "- Гражданский кодекс РФ (Глава 37 — Подряд, ст. 702-729)\n"
            "- Жилищный кодекс РФ (ст. 161 — обязанности УК, ст. 162 — договор управления)\n"
            "- ПП РФ №416 (порядок приёмки работ)\n"
            "- ПП РФ №354 (сроки устранения аварий)\n"
            "- Правила технической эксплуатации жилищного фонда\n\n"
            f"{self.get_role_instruction(role)}"
        )
    
        return (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )

class HistoryAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("История заявок", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "история": {
                "synonyms": ["архив", "лог", "хронология", "прошлые заявки", "ранее поданные заявки"],
                "norm_refs": ["ФЗ №209-ФЗ", "ПП РФ №731, п. 3"],
                "contexts": ["хранение", "доступ", "сроки хранения", "экспорт", "удаление"]
            },
            "когда": {
                "synonyms": ["дата", "время", "срок", "период", "вчера", "неделю назад", "в прошлом месяце"],
                "norm_refs": ["ПП РФ №731, п. 3(3)", "ФЗ №59-ФЗ, ст. 12"],
                "contexts": ["фильтрация по дате", "поиск по периоду", "история за год", "ограничения по срокам"]
            },
            "было": {
                "synonyms": ["происходило", "случалось", "фиксировали", "регистрировали", "учитывали"],
                "norm_refs": ["ФЗ №209-ФЗ", "ПП РФ №731"],
                "contexts": ["подтверждение факта", "документальное подтверждение", "акты", "скриншоты"]
            },
            "прошлый": {
                "synonyms": ["предыдущий", "ранее", "старый", "закрытый", "выполненный"],
                "norm_refs": ["ПП РФ №731, Приложение 1"],
                "contexts": ["статус заявки", "результат выполнения", "оценка качества", "жалобы по прошлым заявкам"]
            },
            "делали": {
                "synonyms": ["выполняли", "проводили", "устраняли", "ремонтировали", "обслуживали"],
                "norm_refs": ["ЖК РФ, ст. 162", "ПП РФ №731, Приложение 1"],
                "contexts": ["описание работ", "фотоотчёт", "акт выполненных работ", "гарантийный срок"]
            },
            "ремонтировали": {
                "synonyms": ["чинили", "восстанавливали", "заменяли", "обновляли", "модернизировали"],
                "norm_refs": ["ГК РФ, ст. 753", "ПП РФ №731, Приложение 1"],
                "contexts": ["перечень работ", "использованные материалы", "сроки гарантии", "повторные обращения"]
            },
            "личный кабинет": {
                "synonyms": ["портал госуслуг", "гис жкх", "мобильное приложение", "сайт ук", "онлайн-сервис"],
                "norm_refs": ["ПП РФ №731, п. 3(2)", "ФЗ №209-ФЗ"],
                "contexts": ["авторизация", "просмотр истории", "экспорт данных", "уведомления", "жалобы"]
            },
            "обращение в ук": {
                "synonyms": ["запрос в управляющую компанию", "письменный запрос", "электронное обращение", "жалоба"],
                "norm_refs": ["ФЗ №59-ФЗ, ст. 12", "ПП РФ №731, п. 10"],
                "contexts": ["срок ответа (30 дней)", "обязательность предоставления", "отказ в выдаче", "обжалование"]
            },
            "статус заявки": {
                "synonyms": ["в работе", "выполнено", "отклонено", "ожидает", "закрыто", "передано подрядчику"],
                "norm_refs": ["ПП РФ №731, Приложение 1", "Правила технической эксплуатации ЖКХ"],
                "contexts": ["отслеживание в реальном времени", "уведомления", "сроки выполнения", "просрочки"]
            },
            "доступ к данным": {
                "synonyms": ["право на информацию", "запрос данных", "копия истории", "выгрузка", "экспорт в Excel"],
                "norm_refs": ["ФЗ №59-ФЗ, ст. 12", "ФЗ №152-ФЗ, ст. 8"],
                "contexts": ["согласие на обработку", "безопасность", "формат предоставления", "электронная подпись"]
            },
            "система учета": {
                "synonyms": ["crm жкх", "диспетчерская система", "1с жкх", "внутренняя база ук", "erz", "егисжкх"],
                "norm_refs": [],
                "contexts": ["интеграция с ГИС ЖКХ", "резервное копирование", "аудит", "техническая поддержка"]
            },
            "жалоба по истории": {
                "synonyms": ["не отвечают", "скрывают данные", "нет в истории", "ошибки в записях", "фальсификация"],
                "norm_refs": ["ФЗ №59-ФЗ, ст. 12", "ПП РФ №731, п. 10"],
                "contexts": ["досудебная претензия", "жалоба в ГЖИ", "штрафы для УК", "судебная практика"]
            },
            "экспорт истории": {
                "synonyms": ["скачать историю", "получить выписку", "распечатать", "сохранить pdf", "выгрузить в excel"],
                "norm_refs": ["ФЗ №59-ФЗ", "ФЗ №152-ФЗ"],
                "contexts": ["форматы файлов", "электронная подпись", "ограничения", "технические требования"]
            },
            "срок хранения": {
                "synonyms": ["архивирование", "удаление данных", "давность", "период хранения", "резервные копии"],
                "norm_refs": ["ФЗ №152-ФЗ, ст. 21", "ПП РФ №731, п. 3(3)"],
                "contexts": ["3 года", "5 лет", "бессрочно", "по требованию контролирующих органов"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "dom.gosuslugi.ru", "gjirf.ru", "roscomnadzor.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".gosuslugi.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ФЗ 209-ФЗ история заявок")
        queries.append(f"{query} ПП РФ 731 раскрытие информации")
        queries.append(f"{query} как получить историю заявок ГИС ЖКХ")
        queries.append(f"{query} судебная практика по отказу в предоставлении истории заявок")
        queries.append(f"{query} срок хранения заявок ЖКХ")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: История заявок в ЖКХ
        Формирует системный промт для Saiga/LLaMA-3 8B:
        - Фокус: доступ к истории заявок, ГИС ЖКХ, порядок запроса, раскрытие информации
        - Жёсткая структура и ссылки на нормативные акты
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        penalty_keywords = [
            "пени", "пеня", "неустойка", "штраф за просрочку",
            "ставка цб", "ключевая ставка", "расчет пени"
        ]
        should_calculate_penalty = any(kw in summary.lower() for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — эксперт по истории заявок и информационным системам ЖКХ. "
            "Дай точный, структурированный и юридически корректный ответ, используя ТОЛЬКО контекст, веб-результаты и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если данных нет — отвечай: 'Недостаточно данных для точного ответа.'\n"
            "2. Подкрепляй все утверждения ссылками на нормативные акты ([ФЗ №59-ФЗ, ст. 12], [ПП РФ №731, п. 3]).\n"
            "3. Структура ответа: Краткий вывод → Нормативное обоснование → Пошаговая инструкция → Судебная практика.\n"
            "4. Формулы пени только при наличии ключевых слов.\n"
            "5. Приоритет источников: ФЗ > ПП РФ > внутренние регламенты УК > судебная практика.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "### Структура ответа:\n"
            "- Краткий вывод (1-2 предложения: что делать немедленно или по закону)\n"
            "- Нормативное обоснование (ФЗ, ПП РФ, внутренние регламенты)\n"
            "- Пошаговая инструкция:\n"
            "  * Где хранится история заявок (ГИС ЖКХ, личный кабинет, внутренняя система УК — ПП РФ №731, п. 3)\n"
            "  * Как получить доступ (авторизация, письменный запрос — ФЗ №59-ФЗ, ст. 12)\n"
            "  * Какая информация доступна (дата, статус, исполнитель, описание — ПП РФ №731, Приложение 1)\n"
            "  * Действия, если данные не предоставляют (жалоба в ГЖИ, прокуратуру, суд — ФЗ №59-ФЗ, ст. 12)\n"
            "- Судебная практика\n"
        )
    
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчет пени (актуальная формула):**\n"
                "- Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Нормативная база: [ЖК РФ, ст. 155.1]\n"
                "- Ограничение: ≤ 9.5% годовых [ФЗ №44-ФЗ, ПП РФ №329]\n"
                "- Начало: с 31-го дня после срока оплаты.\n"
            )
    
        system_prompt += (
            "\n### Ключевые нормативные акты:\n"
            "- ФЗ №209-ФЗ «О раскрытии информации в ЖКХ»\n"
            "- ПП РФ №731 «О стандартизации раскрытия информации»\n"
            "- ФЗ №59-ФЗ «О порядке рассмотрения обращений граждан»\n"
            "- ФЗ №152-ФЗ «О персональных данных»\n\n"
            f"{self.get_role_instruction(role)}"
        )
    
        return (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )

class FallbackAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Fallback", keywords)

        # Триггеры для глупых/провокационных вопросов остаются
        self.trigger_phrases = [
            "дурак", "тупой", "идиот", "чмо", "лох", "придурок", "ненавижу", "не работает",
            "что ты умеешь", "кто ты", "ты кто", "что ты можешь", "для чего ты",
            "зачем ты", "как тебя зовут", "сколько тебе лет", "ты живой", "ты человек",
            "почему ты", "тест", "проверка", "hello", "привет", "здравствуй",
            "эй", "ой", "ага", "ок", "ладно", "понятно", "спасибо", "пожалуйста"
        ]

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "структура жкх": {
                "synonyms": ["органы жкх", "сфера жкх", "жкх расшифровка", "кто в жкх", "организации жкх"],
                "norm_refs": ["ЖК РФ, ст. 161", "ФЗ №189-ФЗ"],
                "contexts": ["управляющие компании", "ТСЖ", "РСО", "государственные органы"]
            },
            "управляющая компания": {
                "synonyms": ["ук", "управляющая организация", "жэк", "жилищная контора", "уо"],
                "norm_refs": ["ЖК РФ, ст. 161-165"],
                "contexts": ["обязанности", "договор управления", "ответственность", "контроль"]
            },
            "тсж": {
                "synonyms": ["товарищество собственников жилья", "тсн", "кооператив", "жск"],
                "norm_refs": ["ЖК РФ, ст. 135-154"],
                "contexts": ["создание", "управление домом", "полномочия", "отчётность"]
            },
            "ресурсоснабжающая организация": {
                "synonyms": ["рсо", "поставщик", "водоканал", "теплосеть", "энергосбыт", "газовая компания"],
                "norm_refs": ["ЖК РФ, ст. 157", "ПП РФ №354"],
                "contexts": ["договоры", "качество услуг", "начисления", "аварии"]
            },
            "госжилинспекция": {
                "synonyms": ["гжи", "жилищная инспекция", "государственная жилищная инспекция", "контролирующий орган"],
                "norm_refs": ["ЖК РФ, ст. 20", "ПП РФ №493"],
                "contexts": ["жалобы", "проверки", "штрафы", "предписания", "обжалование"]
            },
            "фонд капремонта": {
                "synonyms": ["региональный оператор", "фонд содействия реформированию жкх", "оператор капремонта"],
                "norm_refs": ["ЖК РФ, ст. 178", "ФЗ №271-ФЗ"],
                "contexts": ["формирование фонда", "расходование средств", "отчётность", "переход на спецсчёт"]
            },
            "муниципалитет": {
                "synonyms": ["администрация", "местное самоуправление", "горадминистрация", "районная администрация"],
                "norm_refs": ["ЖК РФ, ст. 15", "ФЗ №131-ФЗ"],
                "contexts": ["утверждение тарифов", "программы капремонта", "социальные нормы", "льготы"]
            },
            "роспотребнадзор": {
                "synonyms": ["санэпидстанция", "сэс", "санитарный надзор", "санитарно-эпидемиологическая станция"],
                "norm_refs": ["СанПиН 1.2.3685-21", "ФЗ №52-ФЗ"],
                "contexts": ["замеры температуры", "качество воды", "санитарные нормы", "жалобы"]
            },
            "мчс": {
                "synonyms": ["пожарный надзор", "пожарная инспекция", "пожарная безопасность", "противопожарный режим"],
                "norm_refs": ["ФЗ №69-ФЗ", "ПП РФ №390"],
                "contexts": ["проверки", "предписания", "пожарная сигнализация", "эвакуация"]
            },
            "прокуратура": {
                "synonyms": ["надзор", "защита прав", "обжалование", "генпрокуратура"],
                "norm_refs": ["ФЗ №2202-1", "ЖК РФ, ст. 20"],
                "contexts": ["нарушение прав жильцов", "бездействие УК", "жалобы", "внеочередные проверки"]
            },
            "министерство строительства": {
                "synonyms": ["минстрой", "министерство строительства и жкх", "федеральный орган", "нормативные акты"],
                "norm_refs": [],
                "contexts": ["разъяснения", "приказы", "методические рекомендации", "реформы ЖКХ"]
            },
            "тарифное регулирование": {
                "synonyms": ["региональная служба по тарифам", "рст", "тарифный орган", "установление тарифов"],
                "norm_refs": ["ФЗ №210-ФЗ", "ПП РФ №1149"],
                "contexts": ["расчёт тарифов", "обоснование", "жалобы на тарифы", "ФГИС Тариф"]
            },
            "что такое": {
                "synonyms": ["объясни", "расскажи про", "основы жкх", "кто отвечает за", "кто занимается", "функции", "полномочия"],
                "norm_refs": [],
                "contexts": ["обучающие запросы", "вводные объяснения", "структура", "ответственность"]
            },
            "кто такой": {
                "synonyms": ["чем занимается", "роль", "обязанности", "деятельность", "статус"],
                "norm_refs": [],
                "contexts": ["описание организаций и должностей в ЖКХ"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ generate_fallback_response, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "mchs.gov.ru", "proc.gov.ru", "rosconsumnadzor.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".mchs.gov.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ЖКХ структура")
        queries.append(f"{query} обязанности УК")
        queries.append(f"{query} функции ГЖИ")
        queries.append(f"{query} что такое РСО")
        queries.append(f"{query} основы жилищного законодательства")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def matches(self, query: str) -> bool:
        q = query.lower()
        # 🆕 Основная логика: если запрос содержит любое ключевое слово ИЛИ триггер — ловим
        if any(kw in q for kw in self.keywords):
            return True
        if any(phrase in q for phrase in self.trigger_phrases):
            return True
        # 🆕 Также ловим очень короткие сообщения (1-2 слова), если они не попали под другие агенты
        if len(q.split()) <= 2:
            return True
        return False

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        # Не используем стандартный промпт — ответ генерируется вручную или через LLM в generate_fallback_response
        return ""

    def generate_fallback_response(self, query: str) -> str:
        q = query.lower()

        # 🚫 Грубость / оскорбления — шаблонный ответ
        if any(word in q for word in ["дурак", "тупой", "идиот", "чмо", "лох", "придурок"]):
            return (
                "Я — виртуальный ассистент по вопросам ЖКХ. Меня можно критиковать, но лучше — задать конкретный вопрос. "
                "Например: *«Как передать показания счётчика?»* или *«Куда пожаловаться на протечку?»*. Я помогу!"
            )

        # ❓ Вопросы "что ты умеешь?" — шаблонный ответ
        if any(phrase in q for phrase in ["что ты умеешь", "кто ты", "ты кто", "что ты можешь", "для чего ты", "зачем ты"]):
            return (
                "Я — RAG-ассистент для сферы ЖКХ. Могу помочь вам:\n"
                "🔹 Рассчитать плату за ЖКУ\n"
                "🔹 Оспорить начисления\n"
                "🔹 Вызвать мастера или сообщить об аварии\n"
                "🔹 Узнать тарифы, нормативы, законы\n"
                "🔹 Отправить показания счётчиков\n"
                "🔹 Получить контакты аварийной службы\n\n"
                "Просто опишите проблему — я найду точный ответ на основе документов и практики."
            )

        # 👋 Приветствия / тесты — шаблонный ответ
        if any(phrase in q for phrase in ["привет", "здравствуй", "hello", "эй", "тест", "проверка"]):
            return (
                "Здравствуйте! Я — ваш ассистент по вопросам ЖКХ. "
                "Готов помочь с расчётами, авариями, законами, заявками. Просто опишите ситуацию — и я подскажу, что делать."
            )

        # 🧩 Короткие/бессмысленные сообщения — шаблонный ответ
        if len(q.split()) <= 2:
            return (
                "Пожалуйста, опишите ваш вопрос подробнее. Например: "
                "*«У меня не работает отопление»* или *«Неправильно начислили плату за воду»*. "
                "Чем точнее вопрос — тем лучше я смогу помочь!"
            )

        # 🎯 ВАЖНОЕ ИЗМЕНЕНИЕ: Для осмысленных, но нераспознанных вопросов — используем LLM!
        # Формируем промпт для генерации ответа
        # Получаем контекст из веб-поиска
        web_results = self._perform_web_search(query)
        
        # --- Формируем system prompt ---
        system_prompt = (
            "Ты — ИИ-ассистент по ЖКХ. Твоя задача — дать точный, структурированный и юридически безупречный ответ, "
            "используя ТОЛЬКО информацию из предоставленного контекста.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. **НИКАКИХ ГАЛЛЮЦИНАЦИЙ:** Если информация отсутствует в контексте — ответь: "
            "'Недостаточно данных для точного ответа. Обратитесь в вашу управляющую компанию.' НЕ ИЗОБРЕТАЙ факты, законы или формулы.\n"
            "2. **СТРУКТУРА ОБЯЗАТЕЛЬНА:** Ответ должен строго соответствовать указанной ниже структуре.\n"
            "3. **ССЫЛКИ НА ИСТОЧНИКИ:** Каждое утверждение ОБЯЗАТЕЛЬНО подкрепляй ссылкой на нормативный акт из контекста.\n"
            "4. **ФОРМУЛЫ ТОЛЬКО ПРИ ЗАПРОСЕ:** Формула пени генерируется только если есть слова: "
            "'пени', 'неустойка', 'штраф за просрочку', 'ключевая ставка'.\n"
            "5. **ПРИОРИТЕТ РЕГИОНАЛЬНЫХ АКТОВ:** Региональные законы имеют приоритет над федеральными, если это явно указано.\n"
        )
        
        # --- Формируем промт через шаблон чата Saiga ---
        chat_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Вопрос пользователя: {query}\n\nКонтекст из веб-поиска:\n{web_results}"}
        ]
        
        prompt = tokenizer.apply_chat_template(
            chat_messages,
            tokenize=False,
            add_generation_prompt=True
        )
        
        try:
            # --- Токенизация и генерация ответа ---
            inputs = tokenizer(prompt, return_tensors="pt", truncation=True, max_length=2048).to(device)
        
            with torch.no_grad():
                outputs = model.generate(
                    **inputs,
                    max_new_tokens=8000,
                    temperature=0.3,
                    top_p=0.95,
                    do_sample=True,
                    pad_token_id=tokenizer.eos_token_id
                )
        
            raw_answer = tokenizer.decode(outputs[0], skip_special_tokens=False)
        
            # --- Извлекаем ответ после маркера ассистента ---
            start_marker = "Ассистент:[SEP]"
            start = raw_answer.find(start_marker)
            answer = raw_answer[start + len(start_marker):].strip() if start != -1 else raw_answer.strip()
        
            # --- Очистка стоп-последовательностей ---
            stop_sequences = ["</s>", "Пользователь:", "Ассистент:", "\n\n"]
            for stop in stop_sequences:
                if stop in answer:
                    answer = answer.split(stop)[0].strip()
        
            # --- Проверка информативности ---
            if len(answer.split()) < 5 or any(phrase in answer.lower() for phrase in ["не знаю", "не могу", "извините", "не понимаю"]):
                raise ValueError("Сгенерированный ответ слишком короткий или неинформативный")
        
            return answer
        
        except Exception as e:
            print(f"Ошибка генерации LLM: {e}")
            return (
                "Извините, я не совсем понял ваш запрос. \n\n"
                "Моя специализация — вопросы жилищно-коммунального хозяйства: расчёты, аварийные ситуации, нормативные акты, подача заявок, приборы учёта. \n\n"
                "Пожалуйста, переформулируйте вопрос, и я помогу!"
            )
            
class QualityControlAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Контроль качества услуг", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "качество": {
                "synonyms": ["некачественно", "плохое качество", "нарушение качества", "снижение качества"],
                "norm_refs": ["ПП РФ №354, раздел 6", "СанПиН 1.2.3685-21"],
                "contexts": ["отопление", "вода", "уборка", "санитарное состояние", "шум"]
            },
            "температура в квартире": {
                "synonyms": ["холодно", "сквозняк", "влажность", "не греет", "отопление не работает"],
                "norm_refs": ["СанПиН 1.2.3685-21, п. 9.2", "ПП РФ №354, п. 54(2)"],
                "contexts": ["замер", "акт", "перерасчет", "жалоба", "норматив +18°C"]
            },
            "давление воды": {
                "synonyms": ["слабый напор", "нет напора", "низкое давление", "перебои с водой"],
                "norm_refs": ["СанПиН 1.2.3685-21, п. 9.4", "ПП РФ №354, п. 54(1)"],
                "contexts": ["замер давления", "акт", "перерасчет", "авария", "плановое отключение"]
            },
            "уборка": {
                "synonyms": ["не убирают", "грязно", "пыль", "мусор", "уборка подъезда", "мытье окон", "снег во дворе"],
                "norm_refs": ["ПП РФ №491, п. 12", "Правила технической эксплуатации ЖКХ"],
                "contexts": ["периодичность", "акт проверки", "жалоба в УК", "фото как доказательство"]
            },
            "санитарное состояние": {
                "synonyms": ["воняет", "тараканы", "дезинфекция", "дератизация", "вредители", "насекомые", "протравить"],
                "norm_refs": ["СанПиН 1.2.3685-21, п. 8.1", "ПП РФ №491, п. 12"],
                "contexts": ["обработка", "обязанность УК", "жалоба в Роспотребнадзор", "акт санитарной проверки"]
            },
            "жалоба": {
                "synonyms": ["претензия", "жалобы игнорируются", "регулярные жалобы", "систематические нарушения", "жалоба на УК"],
                "norm_refs": ["ЖК РФ, ст. 161", "ФЗ №59-ФЗ, ст. 12"],
                "contexts": ["письменная форма", "срок ответа 30 дней", "жалоба в ГЖИ/Роспотребнадзор/прокуратуру"]
            },
            "акт": {
                "synonyms": ["акт проверки", "акт о нарушении", "акт выполненных работ", "фото прикладываю", "доказательства"],
                "norm_refs": ["ПП РФ №354, п. 99", "ЖК РФ, ст. 161"],
                "contexts": ["состав комиссии", "обязательные реквизиты", "срок подписания", "односторонний акт"]
            },
            "перерасчёт": {
                "synonyms": ["снижение платы", "компенсация", "возврат средств", "понижение тарифа", "расчёт по формуле"],
                "norm_refs": ["ПП РФ №354, п. 90, Приложение 2", "ЖК РФ, ст. 157"],
                "contexts": ["формула", "период нарушения", "документы для перерасчёта", "сроки начисления"]
            },
            "шум": {
                "synonyms": ["гудит", "вибрация", "стук", "шум в подвале", "лифт гудит"],
                "norm_refs": ["СанПиН 1.2.3685-21, п. 8.3", "ПП РФ №354, п. 54(12)"],
                "contexts": ["замер уровня шума", "акт", "жалоба", "источник шума (лифт, насос)"]
            },
            "оповещение": {
                "synonyms": ["уведомление", "объявление", "информирование", "не предупредили", "не сообщили", "плановое отключение"],
                "norm_refs": ["ПП РФ №354, п. 98(5)", "Правила технической эксплуатации ЖКХ"],
                "contexts": ["срок уведомления (не менее 10 дней)", "способы оповещения", "ответственность за неуведомление"]
            },
            "придомовая территория": {
                "synonyms": ["дорога", "тротуар", "двор", "газон", "парковка", "детская площадка"],
                "norm_refs": ["ПП РФ №491, п. 12", "Правила благоустройства муниципалитета"],
                "contexts": ["уборка", "освещение", "ремонт", "озеленение", "ответственность УК"]
            },
            "систематические нарушения": {
                "synonyms": ["из месяца в месяц", "регулярно не моют", "постоянные перебои", "игнорирование актов"],
                "norm_refs": ["ЖК РФ, ст. 161", "ПП РФ №493"],
                "contexts": ["жалоба в ГЖИ", "проверка", "предписание", "штраф для УК", "расторжение договора управления"]
            },
            "жалоба в контролирующие органы": {
                "synonyms": ["жалоба в прокуратуру", "жалоба в Роспотребнадзор", "проверка ГЖИ", "проверка Роспотребнадзора", "предписание", "штраф для УК"],
                "norm_refs": ["ЖК РФ, ст. 20", "ФЗ №52-ФЗ", "ФЗ №2202-1"],
                "contexts": ["образец жалобы", "сроки рассмотрения", "результаты проверки", "обжалование предписания"]
            },
            "доказательства": {
                "synonyms": ["фото", "видео", "свидетели", "акт", "скриншоты", "переписка"],
                "norm_refs": ["ГПК РФ, ст. 67", "ПП РФ №354, п. 99"],
                "contexts": ["юридическая сила", "приложение к жалобе", "использование в суде", "электронные доказательства"]
            },
            "разъяснительная беседа": {
                "synonyms": ["предупреждение", "предписание", "устное предупреждение", "письменное предупреждение"],
                "norm_refs": ["ПП РФ №493", "ЖК РФ, ст. 20"],
                "contexts": ["меры воздействия на УК", "последствия игнорирования", "фиксация беседы", "повторная проверка"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "rosconsumnadzor.ru", "proc.gov.ru", "vsrf.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".rosconsumnadzor.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ПП РФ 354 раздел 6")
        queries.append(f"{query} СанПиН 1.2.3685-21")
        queries.append(f"{query} перерасчет за некачественную услугу формула")
        queries.append(f"{query} судебная практика по качеству ЖКУ")
        queries.append(f"{query} жалоба в Роспотребнадзор на УК")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Контроль качества услуг ЖКХ
        Формирует системный промт для Saiga/LLaMA-3 8B:
        - Фокус: контроль качества, замеры, параметры, акты, перерасчет, жалобы
        - Жёсткая структура и ссылки на нормативные акты
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        penalty_keywords = [
            "пени", "пеня", "неустойка", "штраф за просрочку",
            "ставка цб", "ключевая ставка", "расчет пени"
        ]
        should_calculate_penalty = any(kw in summary.lower() for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — эксперт по контролю качества коммунальных услуг в ЖКХ. "
            "Дай точный, структурированный и юридически корректный ответ, используя ТОЛЬКО контекст, веб-результаты и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если данных нет — отвечай: 'Недостаточно данных для точного ответа.'\n"
            "2. Подкрепляй все утверждения ссылками на нормативные акты ([ПП РФ №354, п. 58], [СанПиН 1.2.3685-21, п. 9.2]).\n"
            "3. Структура ответа: Краткий вывод → Нормативное обоснование → Пошаговая инструкция → Судебная практика.\n"
            "4. Формулы пени только при наличии ключевых слов.\n"
            "5. Приоритет источников: СанПиН > ПП РФ > ЖК РФ > разъяснения контролирующих органов > судебная практика.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "### Структура ответа:\n"
            "- Краткий вывод (1-2 предложения: что делать немедленно или по закону)\n"
            "- Нормативное обоснование (ПП РФ, СанПиН, ЖК РФ)\n"
            "- Пошаговая инструкция:\n"
            "  * Как зафиксировать нарушение (замер, фото, акт — ПП РФ №354, п. 58, 99)\n"
            "  * Какие параметры считаются нарушением (температура, давление — СанПиН 1.2.3685-21, п. 9.2)\n"
            "  * Как рассчитать перерасчёт (формула из Приложения 2 ПП РФ №354)\n"
            "  * Куда подавать жалобу (УК → ГЖИ → Роспотребнадзор → прокуратура — ЖК РФ, ст. 20)\n"
            "  * Возможные санкции для УК (штраф, предписание, расторжение договора)\n"
            "- Судебная практика\n"
        )
    
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчет пени (актуальная формула):**\n"
                "- Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Нормативная база: [ЖК РФ, ст. 155.1]\n"
                "- Ограничение: ≤ 9.5% годовых [ФЗ №44-ФЗ, ПП РФ №329]\n"
                "- Начало начисления: с 31-го дня после срока оплаты.\n"
            )
    
        system_prompt += (
            "\n### Ключевые нормативные акты:\n"
            "- ПП РФ №354 (качество услуг, замеры, сроки, перерасчёт, акты)\n"
            "- СанПиН 1.2.3685-21 (гигиенические требования к температуре, давлению, шуму)\n"
            "- ЖК РФ (ст. 161 — обязанности УК, ст. 20 — контроль со стороны государства)\n"
            "- ФЗ №59-ФЗ (сроки рассмотрения обращений — 30 дней)\n"
            "- ПП РФ №491 (содержание общего имущества — санитарное состояние)\n\n"
            f"{self.get_role_instruction(role)}"
        )
    
        return (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )
        
class PaymentDocumentsAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Платёжные документы", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "квитанция": {
                "synonyms": ["платёжка", "епд", "платёжный документ", "счёт", "документ об оплате"],
                "norm_refs": ["ПП РФ №354, п. 93-94", "ЖК РФ, ст. 155"],
                "contexts": ["форма", "содержание", "сроки получения", "ошибки", "долг в квитанции"]
            },
            "расшифровка платежа": {
                "synonyms": ["что значит эта строка", "как понять платёжку", "строки в квитанции", "расшифровка епд"],
                "norm_refs": ["ПП РФ №354, Приложение 2", "ПП РФ №491, п. 18"],
                "contexts": ["услуги ЖКХ", "ОДН/КР на СОИ", "пени", "рассрочка", "субсидии"]
            },
            "ошибка в квитанции": {
                "synonyms": ["неправильная сумма", "задвоили оплату", "не пришла оплата", "где долг", "техническая ошибка"],
                "norm_refs": ["ПП РФ №354, п. 95", "ЖК РФ, ст. 157"],
                "contexts": ["жалоба в УК", "акт сверки", "перерасчет", "возврат излишне уплаченного"]
            },
            "единолицевой счет": {
                "synonyms": ["лицевой счёт", "расчётный счёт жильца", "номер лицевого счёта", "единый платёжный документ"],
                "norm_refs": ["ПП РФ №354, п. 93", "ПП РФ №416"],
                "contexts": ["идентификация плательщика", "история платежей", "сверка задолженности", "передача показаний"]
            },
            "ипд": {
                "synonyms": ["индивидуальный платёжный документ", "персонализированный счёт", "платёжка по лицевому счёту"],
                "norm_refs": ["ПП РФ №354, п. 94", "ПП РФ №491"],
                "contexts": ["расчёт по показаниям ИПУ", "персонализация", "расшифровка по услугам", "начисления по нормативу"]
            },
            "жку": {
                "synonyms": ["жилищно-коммунальные услуги", "коммунальные платежи", "оплата за квартиру", "платежи за жильё"],
                "norm_refs": ["ЖК РФ, ст. 154", "ПП РФ №354, раздел 9"],
                "contexts": ["состав платы", "тарифы", "нормативы", "повышающие коэффициенты"]
            },
            "назначение платежа": {
                "synonyms": ["цель платежа", "за что платим", "код услуги", "кбк", "квр"],
                "norm_refs": ["ПП РФ №354, Приложение 2", "ФЗ №54-ФЗ"],
                "contexts": ["идентификация платежа", "возврат средств", "учёт в бухгалтерии", "банковские реквизиты"]
            },
            "реквизиты": {
                "synonyms": ["банковские реквизиты", "инн", "кпп", "расчётный счёт", "бик", "наименование получателя"],
                "norm_refs": ["ПП РФ №354, п. 94(3)", "ФЗ №54-ФЗ"],
                "contexts": ["оплата через банк", "ошибки в реквизитах", "возврат платежа", "чек ккт"]
            },
            "долг в квитанции": {
                "synonyms": ["задолженность", "пени", "неустойка", "просрочка", "сумма долга", "накопленная задолженность"],
                "norm_refs": ["ЖК РФ, ст. 155.1", "ПП РФ №354, п. 94(5)"],
                "contexts": ["расчёт пени", "рассрочка", "ограничение услуг", "списание долга", "ошибочное начисление"]
            },
            "чек": {
                "synonyms": ["кассовый чек", "электронный чек", "фискальный документ", "подтверждение оплаты"],
                "norm_refs": ["ФЗ №54-ФЗ, ст. 4.7", "ПП РФ №354, п. 94(4)"],
                "contexts": ["обязательность выдачи", "электронный формат", "qr-код", "возврат средств", "жалоба при отсутствии"]
            },
            "где долг в квитанции": {
                "synonyms": ["сумма задолженности", "просроченная задолженность", "текущий долг", "история долгов"],
                "norm_refs": ["ПП РФ №354, п. 94(5)", "ЖК РФ, ст. 155"],
                "contexts": ["отдельная строка", "раздел «Задолженность»", "расшифровка по месяцам", "пени отдельной строкой"]
            },
            "провodka": {
                "synonyms": ["бухгалтерская проводка", "отражение в учёте", "учёт платежа", "зачисление оплаты"],
                "norm_refs": [],
                "contexts": ["для бухгалтерии", "юридических лиц", "индивидуальных предпринимателей", "возврат ндс"]
            },
            "способы получения": {
                "synonyms": ["почта", "личный кабинет", "гис жкх", "мобильное приложение", "офис ук", "электронная почта"],
                "norm_refs": ["ПП РФ №354, п. 93(2)", "ФЗ №209-ФЗ"],
                "contexts": ["обязательные способы", "альтернативные каналы", "согласие на электронный документооборот"]
            },
            "сроки предоставления": {
                "synonyms": ["когда приходит квитанция", "дата формирования", "сроки рассылки", "до какого числа"],
                "norm_refs": ["ПП РФ №354, п. 93(1)", "ЖК РФ, ст. 155"],
                "contexts": ["не позднее 1-го числа месяца, следующего за расчётным", "штрафы за нарушение сроков"]
            },
            "жалоба на квитанцию": {
                "synonyms": ["не согласен с начислением", "требую перерасчет", "неправильно начислили", "обжалование платежа"],
                "norm_refs": ["ПП РФ №354, п. 95", "ЖК РФ, ст. 157"],
                "contexts": ["срок подачи — 30 дней", "письменная форма", "акт сверки", "судебное оспаривание"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "dom.gosuslugi.ru", "nalog.gov.ru", "fns.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".nalog.gov.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ПП РФ 354 платёжные документы")
        queries.append(f"{query} ФЗ 54-ФЗ кассовые чеки ЖКХ")
        queries.append(f"{query} расшифровка строк в ЕПД")
        queries.append(f"{query} где долг в квитанции ЖКХ")
        queries.append(f"{query} судебная практика по ошибкам в квитанциях")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Платёжные документы ЖКХ
        Формирует системный промт для Saiga/LLaMA-3 8B:
        - Фокус: квитанции, реквизиты, ошибки, чеки, пени, сроки, формы
        - Жёсткая структура и ссылки на нормативные акты
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        penalty_keywords = [
            "пени", "пеня", "неустойка", "штраф за просрочку",
            "ставка цб", "ключевая ставка", "расчет пени"
        ]
        should_calculate_penalty = any(kw in summary.lower() for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — эксперт по платёжным документам в ЖКХ. "
            "Дай точный, структурированный и юридически корректный ответ, используя ТОЛЬКО контекст, веб-результаты и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если данных нет — отвечай: 'Недостаточно данных для точного ответа.'\n"
            "2. Подкрепляй все утверждения ссылками на нормативные акты ([ПП РФ №354, п. 94], [ФЗ №54-ФЗ, ст. 4.7]).\n"
            "3. Структура ответа: Краткий вывод → Нормативное обоснование → Пошаговая инструкция → Судебная практика.\n"
            "4. Формулы пени только при наличии ключевых слов.\n"
            "5. Приоритет источников: ЖК РФ > ПП РФ > ФЗ №54-ФЗ > разъяснения Минстроя > судебная практика.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "### Структура ответа:\n"
            "- Краткий вывод (1-2 предложения: где искать строку, как исправить ошибку, куда обратиться)\n"
            "- Нормативное обоснование (ЖК РФ, ПП РФ, ФЗ)\n"
            "- Пошаговая инструкция:\n"
            "  * Как выглядит правильная квитанция (обязательные реквизиты — ПП РФ №354, п. 94)\n"
            "  * Где найти долг или пени (раздел «Задолженность» — ПП РФ №354, п. 94(5))\n"
            "  * Как исправить ошибку (жалоба в УК в течение 30 дней — ПП РФ №354, п. 95)\n"
            "  * Как получить чек при оплате (ФЗ №54-ФЗ, ст. 4.7)\n"
            "  * Куда обратиться, если не пришла квитанция (личный кабинет, ГИС ЖКХ, офис УК — ПП РФ №354, п. 93)\n"
        )
    
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчет пени (актуальная формула):**\n"
                "- Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Нормативная база: [ЖК РФ, ст. 155.1]\n"
                "- Ограничение: ≤ 9.5% годовых [ФЗ №44-ФЗ, ПП РФ №329]\n"
                "- Начало начисления: с 31-го дня после срока оплаты.\n"
            )
    
        system_prompt += (
            "\n### Судебная практика:\n"
            "[**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда]\n"
            "Если судебных решений нет: 'Судебная практика по данному вопросу в базе отсутствует'.\n\n"
            "### Ключевые нормативные акты:\n"
            "- ЖК РФ (ст. 155 — сроки и порядок оплаты)\n"
            "- ПП РФ №354 (п. 93-95 — форма, сроки, порядок предоставления и оспаривания платёжных документов)\n"
            "- ФЗ №54-ФЗ «О применении ККТ» (обязательность выдачи чеков при оплате)\n"
            "- ПП РФ №491 (если вопрос касается содержания общего имущества)\n"
            "- ФЗ №209-ФЗ (о раскрытии информации — способы получения документов)\n\n"
            f"{self.get_role_instruction(role)}"
        )
    
        return (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )
        
class BillingAuditAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Аудит начислений", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "аудит квитанции": {
                "synonyms": ["проверка начислений", "анализ платёжки", "аудит ЖКХ", "сверка расчётов", "экспертиза квитанции"],
                "norm_refs": ["ЖК РФ, ст. 158", "ПП РФ №354, п. 95"],
                "contexts": ["пошаговая проверка", "сравнение с нормативами", "расчёт по показаниям", "ошибки в начислениях"]
            },
            "резко выросла плата": {
                "synonyms": ["почему резко выросла плата", "неожиданное повышение", "скачок в квитанции", "внезапное увеличение суммы"],
                "norm_refs": ["ЖК РФ, ст. 154", "ПП РФ №354, п. 42(1)", "ПП РФ №1149"],
                "contexts": ["повышение тарифа", "применение коэффициента 1.5", "начисление по нормативу", "изменение норматива"]
            },
            "непонятные услуги": {
                "synonyms": ["скрытые услуги", "необоснованное начисление", "что это за строка", "расшифровка неясных начислений"],
                "norm_refs": ["ПП РФ №354, Приложение 2", "ПП РФ №491, п. 18"],
                "contexts": ["КР на СОИ", "техническое обслуживание", "дополнительные сборы", "отсутствие расшифровки"]
            },
            "завышенный тариф": {
                "synonyms": ["почему завышено", "не соответствует региональному", "обоснование тарифа", "сравнить тарифы", "переплата"],
                "norm_refs": ["ПП РФ №354, п. 40", "ФЗ №210-ФЗ", "ПП РФ №1149"],
                "contexts": ["региональный тариф", "ФГИС Тариф", "жалоба в ФАС", "расчёт по среднему", "ошибочное применение"]
            },
            "повышающий коэффициент": {
                "synonyms": ["коэффициент 1.5", "повышающий множитель", "штрафной коэффициент", "начисление с надбавкой"],
                "norm_refs": ["ПП РФ №354, п. 42(1)", "ПП РФ №354, п. 81(12)"],
                "contexts": ["отсутствие ИПУ", "истёк срок поверки", "отказ в допуске к поверке", "неправомерное применение"]
            },
            "проверка УК": {
                "synonyms": ["аудит управляющей компании", "ревизия начислений", "проверка расчётов УК", "жалоба на УК"],
                "norm_refs": ["ЖК РФ, ст. 161", "ПП РФ №493"],
                "contexts": ["запрос документов", "акт сверки", "жалоба в ГЖИ", "внеплановая проверка", "штрафы для УК"]
            },
            "аномалия в расчёте": {
                "synonyms": ["неверные начисления", "ошибка в квитанции", "техническая ошибка", "баг в системе", "дублирование платежей"],
                "norm_refs": ["ПП РФ №354, п. 95", "ЖК РФ, ст. 157"],
                "contexts": ["акт сверки", "перерасчёт", "возврат излишне уплаченного", "жалоба в прокуратуру"]
            },
            "детализация расчёта": {
                "synonyms": ["расшифровка начислений", "формула расчёта", "как считали", "обоснование суммы", "расчёт по месяцам"],
                "norm_refs": ["ПП РФ №354, Приложение 2", "ЖК РФ, ст. 158"],
                "contexts": ["запрос в УК", "обязательность предоставления", "электронный формат", "срок ответа 10 дней"]
            },
            "сравнить тарифы": {
                "synonyms": ["тариф по региону", "официальный тариф", "где проверить тариф", "сайт ФАС", "ФГИС Тариф"],
                "norm_refs": ["ФЗ №210-ФЗ", "ПП РФ №1149"],
                "contexts": ["региональный тарифный орган", "публичная база", "жалоба при несоответствии", "обоснование начислений"]
            },
            "жалоба в ГЖИ": {
                "synonyms": ["обращение в жилинспекцию", "проверка ГЖИ", "предписание УК", "штраф для УК", "внеплановая проверка"],
                "norm_refs": ["ЖК РФ, ст. 20", "ПП РФ №493"],
                "contexts": ["образец жалобы", "срок рассмотрения 30 дней", "результаты проверки", "обжалование предписания"]
            },
            "судебное оспаривание": {
                "synonyms": ["исковое заявление", "взыскание излишне уплаченного", "компенсация морального вреда", "суд по ЖКХ"],
                "norm_refs": ["ГК РФ, ст. 1064", "ГПК РФ, ст. 131"],
                "contexts": ["доказательства", "расчёт убытков", "независимая экспертиза", "госпошлина", "срок исковой давности"]
            },
            "начисление по нормативу": {
                "synonyms": ["расчёт без счётчика", "норматив потребления", "объём по норме", "если не передали показания"],
                "norm_refs": ["ПП РФ №354, п. 42", "ПП РФ №354, п. 59"],
                "contexts": ["условия применения", "период действия", "перерасчёт после передачи показаний", "ошибки в объёме"]
            },
            "акт сверки": {
                "synonyms": ["акт проверки начислений", "сверка счётчиков", "акт обследования", "подтверждение показаний"],
                "norm_refs": ["ПП РФ №354, п. 95", "ЖК РФ, ст. 157"],
                "contexts": ["состав комиссии", "обязательные реквизиты", "срок подписания", "использование в суде"]
            },
            "возврат излишне уплаченного": {
                "synonyms": ["переплата", "возврат средств", "зачёт в счёт будущих платежей", "компенсация"],
                "norm_refs": ["ЖК РФ, ст. 157", "ГК РФ, ст. 1102"],
                "contexts": ["заявление на возврат", "срок 5 дней", "безналичный перевод", "жалоба при отказе"]
            },
            "норматив потребления": {
                "synonyms": ["объём по норме", "лимит", "расчёт по нормативу", "утверждённый норматив"],
                "norm_refs": ["ПП РФ №354, п. 21", "ПП РФ №306"],
                "contexts": ["региональные различия", "сезонные коэффициенты", "дифференцированные нормативы", "проверка актуальности"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "fstrf.ru", "gjirf.ru", "vsrf.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".fstrf.ru", ".vsrf.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ПП РФ 354 аудит начислений")
        queries.append(f"{query} ЖК РФ ст 158 проверка квитанции")
        queries.append(f"{query} повышающий коэффициент 1.5 законно")
        queries.append(f"{query} судебная практика по оспариванию начислений ЖКХ")
        queries.append(f"{query} как проверить правильность начислений за ЖКХ")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Аудит начислений ЖКХ
        Формирует системный промт:
        - Фокус: аудит и проверка начислений, запросы, расчёты, оспаривание, документы, ГЖИ
        - Жёсткая структура и ссылки на нормативные акты
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        penalty_keywords = [
            "пени", "пеня", "неустойка", "штраф за просрочку",
            "ставка цб", "ключевая ставка", "расчет пени"
        ]
        should_calculate_penalty = any(kw in summary.lower() for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — эксперт по аудиту начислений в ЖКХ. "
            "Дай точный, структурированный и юридически корректный ответ, используя ТОЛЬКО контекст, веб-результаты и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если данных нет — отвечай: 'Недостаточно данных для точного ответа.'\n"
            "2. Подкрепляй все утверждения ссылками на нормативные акты ([ЖК РФ, ст. 158], [ПП РФ №354, п. 95]).\n"
            "3. Структура ответа: Краткий вывод → Нормативное обоснование → Пошаговая инструкция по аудиту → Судебная практика.\n"
            "4. Формулы пени только при наличии ключевых слов.\n"
            "5. Приоритет источников: ЖК РФ > ПП РФ > разъяснения Минстроя > судебная практика.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "### Структура ответа:\n"
            "- Краткий вывод (1-2 предложения: что делать, куда обратиться, законно ли начисление)\n"
            "- Нормативное обоснование (ЖК РФ, ПП РФ, ссылки на разделы по расчёту, проверке, оспариванию)\n"
            "- Пошаговая инструкция по аудиту:\n"
            "  * Как запросить детализацию расчёта (письменный запрос в УК — ЖК РФ, ст. 158)\n"
            "  * Как проверить правильность начислений (сравнение с тарифами, ИПУ, нормативами — ПП РФ №354, разделы 4,5)\n"
            "  * Как оспорить начисление (претензия → жалоба в ГЖИ → суд — ЖК РФ, ст. 158)\n"
            "  * Какие документы собрать (квитанции, акты, договоры — ПП РФ №354, п. 95)\n"
            "  * Что делать при отказе УК (жалоба в ГЖИ с приложением документов — ПП РФ №493)\n"
        )
    
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчет пени (актуальная формула):**\n"
                "- Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Нормативная база: [ЖК РФ, ст. 155.1]\n"
                "- Ограничение: ≤ 9.5% годовых [ФЗ №44-ФЗ, ПП РФ №329]\n"
                "- Начало начисления: с 31-го дня после срока оплаты.\n"
            )
    
        system_prompt += (
            "\n### Судебная практика:\n"
            "[**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда]\n"
            "Если судебных решений нет: 'Судебная практика по данному вопросу в базе отсутствует'.\n\n"
            "### Ключевые нормативные акты:\n"
            "- ЖК РФ (ст. 154-158 — порядок расчёта и оспаривания)\n"
            "- ПП РФ №354 (разделы 4,5,9 — расчёт по нормативу, по ИПУ, порядок проверки)\n"
            "- ПП РФ №491 (содержание общего имущества, при необходимости)\n"
            "- ПП РФ №1149 (тарифное регулирование, ФГИС Тариф)\n"
            "- ПП РФ №493 (проверки ГЖИ)\n\n"
            f"{self.get_role_instruction(role)}"
        )
    
        return (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )
        
class SubsidyAndBenefitsAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Льготы и субсидии", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "льгота": {
                "synonyms": ["скидка", "компенсация", "преференция", "льготное начисление", "федеральная льгота", "региональная льгота"],
                "norm_refs": ["ЖК РФ, ст. 159", "ФЗ №181-ФЗ", "ФЗ №5-ФЗ"],
                "contexts": ["инвалиды", "ветераны", "многодетные", "чернобыльцы", "ветераны труда"]
            },
            "субсидия": {
                "synonyms": ["пособие на ЖКХ", "материальная помощь", "господдержка", "субсидия на оплату ЖКУ"],
                "norm_refs": ["ЖК РФ, ст. 159", "ПП РФ №761"],
                "contexts": ["доход ниже прожиточного", "расчёт по формуле", "ежемесячная выплата", "семейный доход"]
            },
            "инвалид": {
                "synonyms": ["инвалиды", "ограниченные возможности", "льготы для инвалидов", "социальная защита"],
                "norm_refs": ["ФЗ №181-ФЗ, ст. 17", "ЖК РФ, ст. 159"],
                "contexts": ["скидка 50%", "расчёт по нормативу", "технические условия", "сопровождающие лица"]
            },
            "ветеран": {
                "synonyms": ["ветераны", "ветераны труда", "ветераны боевых действий", "пенсионеры-ветераны"],
                "norm_refs": ["ФЗ №5-ФЗ, ст. 21", "ФЗ №76-ФЗ"],
                "contexts": ["скидка 50%", "региональные доплаты", "компенсация части платы", "льготы по нормативу"]
            },
            "многодетный": {
                "synonyms": ["многодетные", "семья с тремя детьми", "дети до 18 лет", "льготы для многодетных семей"],
                "norm_refs": ["Указ Президента №431", "региональные законы"],
                "contexts": ["скидка 30-50%", "бесплатное ЖКХ", "льготы по нормативам", "региональные программы"]
            },
            "доход ниже прожиточного": {
                "synonyms": ["малоимущий", "низкий доход", "доход на члена семьи", "расчёт субсидии", "формула субсидии"],
                "norm_refs": ["ПП РФ №761, п. 7", "ЖК РФ, ст. 159"],
                "contexts": ["порог 22% расходов", "учёт всех доходов", "документы о доходах", "справка 2-НДФЛ"]
            },
            "оформление льготы": {
                "synonyms": ["как оформить льготу", "куда подавать документы", "документы для льготы", "заявление на льготу"],
                "norm_refs": ["ПП РФ №761, п. 10", "ФЗ №181-ФЗ, ст. 17"],
                "contexts": ["МФЦ", "ГИС ЖКХ", "портал госуслуг", "соцзащита", "срок рассмотрения 10 дней"]
            },
            "оформление субсидии": {
                "synonyms": ["как оформить субсидию", "документы для субсидии", "заявление на субсидию", "пособие на оплату ЖКХ"],
                "norm_refs": ["ПП РФ №761, п. 10-15", "ЖК РФ, ст. 159"],
                "contexts": ["МФЦ", "ГИС ЖКХ", "срок действия 6 месяцев", "ежегодное подтверждение", "справка о составе семьи"]
            },
            "отказ в льготе": {
                "synonyms": ["отказ в субсидии", "приостановление субсидии", "лишение льготы", "аннулирование"],
                "norm_refs": ["ПП РФ №761, п. 23", "ФЗ №181-ФЗ, ст. 17"],
                "contexts": ["не предоставлены документы", "изменение дохода", "неуплата ЖКХ", "обжалование отказа"]
            },
            "перерасчет субсидии": {
                "synonyms": ["перерасчёт субсидии", "изменение размера", "корректировка выплаты", "доначисление субсидии"],
                "norm_refs": ["ПП РФ №761, п. 20", "ЖК РФ, ст. 159"],
                "contexts": ["изменение состава семьи", "изменение дохода", "изменение тарифов", "заявление о перерасчёте"]
            },
            "возврат излишне выплаченной субсидии": {
                "synonyms": ["возврат субсидии", "излишне выплаченная сумма", "требуют вернуть субсидию", "долг по субсидии"],
                "norm_refs": ["ПП РФ №761, п. 24", "ГК РФ, ст. 1102"],
                "contexts": ["ошибка расчёта", "скрытие доходов", "добровольный возврат", "взыскание через суд"]
            },
            "льгота по оплате": {
                "synonyms": ["скидка на оплату", "компенсация части платы", "льгота по нормативу", "расчёт с учётом льготы"],
                "norm_refs": ["ЖК РФ, ст. 159", "ФЗ №181-ФЗ, ст. 17"],
                "contexts": ["50% от суммы", "по нормативу потребления", "без учёта повышающего коэффициента", "на определённые услуги"]
            },
            "региональная доплата": {
                "synonyms": ["дополнительные льготы", "региональные программы", "местные преференции", "доплаты от субъекта"],
                "norm_refs": ["региональные законы", "Указ Президента №431"],
                "contexts": ["размер зависит от региона", "дополнительно к федеральным", "требуется отдельное заявление", "условия могут отличаться"]
            },
            "жилищные сертификаты": {
                "synonyms": ["жилищная субсидия", "госпрограмма", "молодая семья", "переселение", "расселение аварийного жилья"],
                "norm_refs": ["ФЗ №185-ФЗ", "ПП РФ №1177"],
                "contexts": ["целевое использование", "расчёт площади", "очередь", "документы для участия"]
            },
            "жалоба на отказ": {
                "synonyms": ["обжалование отказа", "досудебная претензия", "жалоба в прокуратуру", "исковое заявление"],
                "norm_refs": ["ФЗ №59-ФЗ, ст. 12", "ГПК РФ, ст. 131"],
                "contexts": ["срок 30 дней", "приложение документов", "решение суда", "взыскание морального вреда"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "gosuslugi.ru", "pfr.gov.ru", "socmin.ru", "vsrf.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".gosuslugi.ru", ".pfr.gov.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ПП РФ 761 субсидии ЖКХ")
        queries.append(f"{query} ЖК РФ ст 159 льготы")
        queries.append(f"{query} ФЗ 181-ФЗ льготы инвалидам")
        queries.append(f"{query} судебная практика по отказу в субсидии")
        queries.append(f"{query} как оформить субсидию через ГИС ЖКХ")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Льготы и субсидии ЖКХ
        Формирует системный промт:
        - Фокус: льготы, субсидии, компенсации — категории, документы, подача, сроки, обжалование
        - Жёсткая структура и ссылки на нормативные акты
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        penalty_keywords = [
            "пени", "пеня", "неустойка", "штраф за просрочку",
            "ставка цб", "ключевая ставка", "расчет пени"
        ]
        should_calculate_penalty = any(kw in summary.lower() for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — эксперт по льготам, субсидиям и компенсациям ЖКХ. "
            "Дай точный, структурированный и юридически корректный ответ, используя ТОЛЬКО контекст, веб-результаты и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если данных нет — отвечай: 'Недостаточно данных для точного ответа.'\n"
            "2. Подкрепляй все утверждения ссылками на нормативные акты ([ЖК РФ, ст. 159], [ПП РФ №761, п. 10], [ФЗ №181-ФЗ, ст. 5]).\n"
            "3. Структура ответа: Краткий вывод → Нормативное обоснование → Пошаговая инструкция → Судебная практика.\n"
            "4. Формулы пени только при наличии ключевых слов.\n"
            "5. Приоритет источников: ЖК РФ > ПП РФ > ФЗ > региональные акты > судебная практика.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "### Структура ответа:\n"
            "- Краткий вывод (1-2 предложения: имеет ли право, куда обращаться, что делать при отказе)\n"
            "- Нормативное обоснование (ЖК РФ, ПП РФ, ФЗ, ссылки на разделы по льготам и субсидиям)\n"
            "- Пошаговая инструкция:\n"
            "  * Кто имеет право? (категории граждан — ЖК РФ, ст. 159, ФЗ №181-ФЗ)\n"
            "  * Какие документы нужны? (справки о доходах, составе семьи, удостоверения — ПП РФ №761, п. 10)\n"
            "  * Куда подавать? (МФЦ, ГИС ЖКХ, портал госуслуг, соцзащита — ПП РФ №761)\n"
            "  * Сроки рассмотрения и выплаты (10 рабочих дней — ПП РФ №761, п. 15)\n"
            "  * Что делать при отказе? (жалоба в вышестоящий орган, прокуратуру, суд — ФЗ №59-ФЗ, ст. 12)\n"
        )
    
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчет пени (актуальная формула):**\n"
                "- Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Нормативная база: [ЖК РФ, ст. 155.1]\n"
                "- Ограничение: ≤ 9.5% годовых [ФЗ №44-ФЗ, ПП РФ №329]\n"
                "- Начало начисления: с 31-го дня после срока оплаты.\n"
            )
    
        system_prompt += (
            "\n### Судебная практика:\n"
            "[**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда]\n"
            "Если судебных решений нет: 'Судебная практика по данному вопросу в базе отсутствует'.\n\n"
            "### Ключевые нормативные акты:\n"
            "- ЖК РФ (ст. 159-160 — основания и порядок предоставления льгот)\n"
            "- ПП РФ №761 «О предоставлении субсидий на оплату ЖКУ»\n"
            "- ФЗ №181-ФЗ «О социальной защите инвалидов»\n"
            "- ФЗ №5-ФЗ «О ветеранах»\n"
            "- Указ Президента РФ №431 «О мерах по социальной поддержке многодетных семей»\n"
            "- Региональные законы и постановления (если есть в контексте)\n\n"
            f"{self.get_role_instruction(role)}"
        )
    
        return (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )
        
class LegalClaimsAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Юридические претензии", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "претензия": {
                "synonyms": ["досудебная претензия", "требование", "письменное обращение", "образец претензии", "претензионный порядок"],
                "norm_refs": ["ЖК РФ, ст. 162", "ГК РФ, ст. 452"],
                "contexts": ["обязательность", "срок ответа 30 дней", "реквизиты", "регистрация", "последствия игнорирования"]
            },
            "иск": {
                "synonyms": ["исковое заявление", "судебный иск", "образец иска", "подача в суд", "гражданский иск"],
                "norm_refs": ["ГПК РФ, ст. 131", "ЖК РФ, ст. 158"],
                "contexts": ["подсудность", "госпошлина", "приложения", "расчёт требований", "исковая давность"]
            },
            "жалоба в прокуратуру": {
                "synonyms": ["обращение в прокуратуру", "проверка прокуратуры", "надзор прокурора", "внеочередная проверка"],
                "norm_refs": ["ФЗ №2202-1, ст. 21", "ЖК РФ, ст. 20"],
                "contexts": ["образец жалобы", "срок рассмотрения 30 дней", "результаты проверки", "вступление в дело"]
            },
            "взыскание": {
                "synonyms": ["взыскание долга", "принудительное исполнение", "исполнительное производство", "арест имущества", "запрет выезда"],
                "norm_refs": ["ФЗ №229-ФЗ", "ГПК РФ, ст. 428"],
                "contexts": ["судебный приказ", "исполнительный лист", "приставы", "сроки исполнения", "обжалование действий пристава"]
            },
            "неустойка": {
                "synonyms": ["пени", "штраф за просрочку", "финансовая санкция", "расчёт неустойки", "проценты за неисполнение"],
                "norm_refs": ["ЖК РФ, ст. 155.1", "ГК РФ, ст. 330"],
                "contexts": ["формула расчёта", "максимальный размер", "взыскание через суд", "уменьшение судом"]
            },
            "моральный вред": {
                "synonyms": ["компенсация морального вреда", "нравственные страдания", "компенсация за стресс", "нематериальный ущерб"],
                "norm_refs": ["ГК РФ, ст. 151", "ФЗ №230-ФЗ"],
                "contexts": ["доказательства страданий", "размер компенсации", "взыскание с УК", "судебная практика"]
            },
            "досудебное урегулирование": {
                "synonyms": ["претензионный порядок", "обязательная претензия", "попытка мирного урегулирования", "до обращения в суд"],
                "norm_refs": ["ЖК РФ, ст. 162", "ГК РФ, ст. 452"],
                "contexts": ["обязательно для ЖКХ", "срок 30 дней", "регистрация входящей корреспонденции", "подтверждение вручения"]
            },
            "жалоба в ГЖИ": {
                "synonyms": ["обращение в жилинспекцию", "проверка ГЖИ", "предписание УК", "штраф для УК", "внеплановая проверка"],
                "norm_refs": ["ЖК РФ, ст. 20", "ПП РФ №493"],
                "contexts": ["образец жалобы", "срок рассмотрения 30 дней", "акт проверки", "обжалование предписания"]
            },
            "обращение в Роспотребнадзор": {
                "synonyms": ["жалоба в Роспотребнадзор", "проверка Роспотребнадзора", "санитарные нормы", "качество услуг"],
                "norm_refs": ["ФЗ №52-ФЗ", "СанПиН 1.2.3685-21"],
                "contexts": ["замеры температуры/давления", "акт санитарной проверки", "предписание", "ответ в течение 30 дней"]
            },
            "госпошлина": {
                "synonyms": ["судебный сбор", "оплата иска", "квитанция госпошлины", "льготы по госпошлине", "рассрочка госпошлины"],
                "norm_refs": ["НК РФ, ст. 333.19", "ФЗ №2202-1"],
                "contexts": ["расчёт по сумме иска", "оплата через банк", "возврат при отказе", "льготы для инвалидов/ветеранов"]
            },
            "подсудность": {
                "synonyms": ["какой суд", "районный суд", "мировой суд", "место подачи иска", "территориальная подсудность"],
                "norm_refs": ["ГПК РФ, ст. 28-32"],
                "contexts": ["по месту нахождения ответчика", "по месту жительства истца", "имущественные споры", "цена иска"]
            },
            "доказательства": {
                "synonyms": ["свидетельские показания", "нотариальные документы", "фото", "видео", "акты", "экспертиза", "переписка"],
                "norm_refs": ["ГПК РФ, ст. 67", "ФЗ №446-ФЗ"],
                "contexts": ["юридическая сила", "нотариальное заверение", "независимая экспертиза", "электронные доказательства"]
            },
            "судебный приказ": {
                "synonyms": ["упрощённое взыскание", "приказное производство", "без судебного заседания", "взыскание по долгам"],
                "norm_refs": ["ГПК РФ, ст. 122", "ФЗ №229-ФЗ"],
                "contexts": ["сумма до 500 тыс. руб.", "возражения должника", "отмена приказа", "исполнительный лист"]
            },
            "ходатайство": {
                "synonyms": ["заявление в суд", "просьба суда", "обеспечение иска", "приобщение доказательств", "назначение экспертизы"],
                "norm_refs": ["ГПК РФ, ст. 148", "АПК РФ, ст. 71"],
                "contexts": ["письменная форма", "сроки подачи", "обязательность рассмотрения", "удовлетворение/отказ"]
            },
            "срок исковой давности": {
                "synonyms": ["исковая давность", "срок предъявления иска", "пропущенный срок", "восстановление срока", "3 года"],
                "norm_refs": ["ГК РФ, ст. 196", "ГК РФ, ст. 200"],
                "contexts": ["3 года для жилищных споров", "начало течения", "приостановление", "восстановление по уважительным причинам"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "proc.gov.ru", "vsrf.ru", "sudrf.ru", "fssprus.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".vsrf.ru", ".sudrf.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ГПК РФ ст 131 исковое заявление")
        queries.append(f"{query} ЖК РФ ст 162 претензия УК")
        queries.append(f"{query} судебная практика по моральному вреду ЖКХ")
        queries.append(f"{query} образец жалобы в прокуратуру на УК")
        queries.append(f"{query} срок исковой давности жилищные споры")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Юридические претензии ЖКХ
        Формирует системный промт:
        - Фокус: жилищные споры — претензии, доказательства, иск, подсудность, исковая давность, судебная практика
        - Жёсткая структура и ссылки на нормативные акты
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        penalty_keywords = [
            "пени", "пеня", "неустойка", "штраф за просрочку",
            "ставка цб", "ключевая ставка", "расчет пени"
        ]
        should_calculate_penalty = any(kw in summary.lower() for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — эксперт по жилищным спорам. Дай точный, структурированный и юридически корректный ответ, "
            "используя ТОЛЬКО контекст, веб-результаты и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если данных нет — отвечай: 'Недостаточно данных для точного ответа.'\n"
            "2. Структура ответа: Краткий вывод → Нормативное обоснование → Пошаговая инструкция → Судебная практика.\n"
            "3. Подкрепляй каждое утверждение ссылками на нормативные акты ([ГК РФ, ст. 330], [ЖК РФ, ст. 162], [ГПК РФ, ст. 131]).\n"
            "4. Формулы пени только при наличии ключевых слов.\n"
            "5. Приоритет источников: ГК РФ > ГПК РФ > ЖК РФ > ФЗ > ПП РФ > судебная практика.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "### Структура ответа:\n"
            "- Краткий вывод (1-2 предложения: что делать, куда подавать, сроки, шансы на успех)\n"
            "- Нормативное обоснование (ГК РФ, ГПК РФ, ЖК РФ, ФЗ, ссылки на статьи)\n"
            "- Пошаговая инструкция:\n"
            "  * Досудебное урегулирование: составление претензии (ЖК РФ, ст. 162)\n"
            "  * Сбор доказательств: акты, фото, переписка, свидетели (ГПК РФ, ст. 67)\n"
            "  * Подача жалобы: ГЖИ, Роспотребнадзор, прокуратура (ФЗ №59-ФЗ, ст. 12)\n"
            "  * Подача иска: подсудность, госпошлина, приложения (ГПК РФ, ст. 131)\n"
            "  * Сроки: исковая давность — 3 года (ГК РФ, ст. 196), рассмотрение претензии — 30 дней (ЖК РФ, ст. 162)\n"
        )
    
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчет пени (актуальная формула):**\n"
                "- Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Нормативная база: [ЖК РФ, ст. 155.1]\n"
                "- Ограничение: ≤ 9.5% годовых [ФЗ №44-ФЗ, ПП РФ №329]\n"
                "- Начало начисления: с 31-го дня после срока оплаты.\n"
            )
    
        system_prompt += (
            "\n### Судебная практика:\n"
            "[**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда]\n"
            "Если судебных решений нет: 'Судебная практика по данному вопросу в базе отсутствует'.\n\n"
            "### Ключевые нормативные акты:\n"
            "- Жилищный кодекс РФ (ст. 155, 158, 161, 162 — претензии, ответственность УК)\n"
            "- Гражданский кодекс РФ (ст. 196 — исковая давность, ст. 330 — неустойка, ст. 151 — моральный вред)\n"
            "- Гражданский процессуальный кодекс РФ (ст. 131 — исковое заявление, ст. 122 — судебный приказ)\n"
            "- ФЗ №59-ФЗ «О порядке рассмотрения обращений граждан»\n"
            "- ФЗ №2202-1 «О прокуратуре РФ»\n"
            "- ПП РФ №354, №491 — по вопросам качества услуг и содержания имущества\n\n"
            f"{self.get_role_instruction(role)}"
        )
    
        return (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )
        
class DebtManagementAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Управление долгами", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "коллектор": {
                "synonyms": ["агент взыскания", "коллекторское агентство", "взыскатель", "письмо от коллектора", "телефонные звонки"],
                "norm_refs": ["ФЗ №230-ФЗ", "ЖК РФ, ст. 158"],
                "contexts": ["права и ограничения", "жалоба на коллектора", "запрет давления", "обращение в ФССП/прокуратуру"]
            },
            "судебный пристав": {
                "synonyms": ["фссп", "пристав-исполнитель", "исполнительное производство", "исполком", "служба судебных приставов"],
                "norm_refs": ["ФЗ №229-ФЗ", "ГПК РФ, ст. 428"],
                "contexts": ["арест счетов", "опись имущества", "запрет выезда", "ограничение прав", "обжалование действий пристава"]
            },
            "реструктуризация долга": {
                "synonyms": ["рассрочка", "план погашения", "график платежей", "соглашение о погашении", "отсрочка платежа"],
                "norm_refs": ["ЖК РФ, ст. 155.1(6)", "ПП РФ №354, п. 69(2)"],
                "contexts": ["заявление в УК", "условия предоставления", "проценты", "льготы", "последствия нарушения графика"]
            },
            "исполнительное производство": {
                "synonyms": ["возбуждение исполнительного производства", "исп. пр-во", "взыскание через приставов", "исполнительный лист"],
                "norm_refs": ["ФЗ №229-ФЗ, ст. 30", "ГПК РФ, ст. 428"],
                "contexts": ["основания", "сроки", "меры принудительного исполнения", "окончание производства", "возврат исполнительного документа"]
            },
            "арест счета": {
                "synonyms": ["блокировка счёта", "списание средств", "удержание с зарплаты", "арест банковских карт", "ограничение операций"],
                "norm_refs": ["ФЗ №229-ФЗ, ст. 70", "ФЗ №102-ФЗ"],
                "contexts": ["размер удержания (до 50%)", "неприкосновенные счета", "жалоба на незаконный арест", "снятие ареста"]
            },
            "запрет выезда": {
                "synonyms": ["ограничение выезда", "запрет на границе", "невыезд", "пограничный запрет", "долг за границей"],
                "norm_refs": ["ФЗ №229-ФЗ, ст. 67", "ФЗ №114-ФЗ"],
                "contexts": ["сумма долга от 10 000 руб.", "уведомление", "снятие запрета после оплаты", "обжалование в суде"]
            },
            "как списать долг": {
                "synonyms": ["списание задолженности", "аннулирование долга", "прощение долга", "истечение срока давности", "банкротство"],
                "norm_refs": ["ГК РФ, ст. 196", "ФЗ №127-ФЗ", "ЖК РФ, ст. 153"],
                "contexts": ["срок исковой давности (3 года)", "банкротство физлица", "реорганизация УК", "ошибка в начислении"]
            },
            "истечение срока давности": {
                "synonyms": ["пропущенный срок", "3 года", "восстановление срока", "применение срока давности", "судебная защита"],
                "norm_refs": ["ГК РФ, ст. 196", "ГК РФ, ст. 200"],
                "contexts": ["3 года для долгов ЖКХ", "начало течения срока", "приостановление", "восстановление по уважительным причинам", "заявление в суд"]
            },
            "банкротство физлица": {
                "synonyms": ["несостоятельность", "процедура банкротства", "реструктуризация долгов", "продажа имущества", "освобождение от долгов"],
                "norm_refs": ["ФЗ №127-ФЗ, гл. X", "ФЗ №229-ФЗ"],
                "contexts": ["долг от 500 000 руб.", "неплатёжеспособность", "финансовый управляющий", "последствия", "освобождение от долгов ЖКХ"]
            },
            "пени": {
                "synonyms": ["неустойка", "штраф за просрочку", "проценты за просрочку", "финансовая санкция", "расчёт пени"],
                "norm_refs": ["ЖК РФ, ст. 155.1", "ПП РФ №329"],
                "contexts": ["формула расчёта", "ограничение до 9.5%", "до 2027 года", "начисление после 30 дней просрочки"]
            },
            "жалоба на действия пристава": {
                "synonyms": ["обжалование постановления", "жалоба старшему приставу", "исковое заявление", "вступление прокурора"],
                "norm_refs": ["ФЗ №229-ФЗ, ст. 128", "КоАП РФ, ст. 30.5"],
                "contexts": ["срок 10 дней", "приоставление исполнения", "возврат средств", "моральный вред"]
            },
            "договор о рассрочке": {
                "synonyms": ["соглашение о погашении", "график платежей", "мировое соглашение", "план реструктуризации"],
                "norm_refs": ["ЖК РФ, ст. 155.1(6)", "ГК РФ, ст. 450"],
                "contexts": ["добровольность", "письменная форма", "регистрация", "последствия нарушения", "изменение условий"]
            },
            "защита от коллекторов": {
                "synonyms": ["запрет звонков", "письменное взаимодействие", "жалоба в Роскомнадзор", "запрет передачи долга"],
                "norm_refs": ["ФЗ №230-ФЗ, ст. 7", "ФЗ №152-ФЗ"],
                "contexts": ["право на запрет", "уведомление коллектора", "ответственность за нарушение", "штрафы"]
            },
            "минимальный прожиточный минимум": {
                "synonyms": ["неприкосновенный минимум", "сумма для жизни", "удержание с зарплаты", "алименты", "социальные выплаты"],
                "norm_refs": ["ФЗ №229-ФЗ, ст. 99", "ФЗ №178-ФЗ"],
                "contexts": ["не может быть арестовано", "расчёт удержаний", "исключения", "жалоба на превышение удержаний"]
            },
            "погашение долга": {
                "synonyms": ["оплата задолженности", "частичное погашение", "зачёт переплаты", "возврат излишне уплаченного", "акт сверки"],
                "norm_refs": ["ЖК РФ, ст. 153", "ГК РФ, ст. 409"],
                "contexts": ["приоритет погашения", "расчёт остатка", "подтверждение оплаты", "снятие ограничений", "обновление данных в ГИС ЖКХ"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "fssprus.ru", "vsrf.ru", "bankrot.fedresurs.ru", "roscomnadzor.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".fssprus.ru", ".vsrf.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ЖКХ ФЗ 229-ФЗ")
        queries.append(f"{query} судебная практика по запрету выезда за долги")
        queries.append(f"{query} как списать долг за ЖКХ через банкротство")
        queries.append(f"{query} образец заявления о рассрочке долга УК")
        queries.append(f"{query} срок исковой давности по долгам ЖКХ")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Управление задолженностью ЖКХ
        Формирует системный промт:
        - Фокус: задолженность, пени, рассрочка, взыскание, исполнительное производство, банкротство
        - Жёсткая структура и ссылки на нормативные акты
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        penalty_keywords = [
            "пени", "пеня", "неустойка", "штраф за просрочку",
            "ставка цб", "ключевая ставка", "расчет пени"
        ]
        should_calculate_penalty = any(kw in summary.lower() for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — эксперт по управлению задолженностью в ЖКХ. Дай точный, структурированный и юридически корректный ответ, "
            "используя ТОЛЬКО контекст, веб-результаты и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если данных нет — отвечай: 'Недостаточно данных для точного ответа.'\n"
            "2. Структура ответа: Краткий вывод → Нормативное обоснование → Пошаговая инструкция → Судебная практика.\n"
            "3. Подкрепляй каждое утверждение ссылками на нормативные акты ([ЖК РФ, ст. 155.1], [ФЗ №229-ФЗ, ст. 69]).\n"
            "4. Формулы пени только при наличии ключевых слов.\n"
            "5. Приоритет источников: ЖК РФ > ФЗ > ПП РФ > судебная практика.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "### Структура ответа:\n"
            "- Краткий вывод (1-2 предложения: что делать, права, сроки)\n"
            "- Нормативное обоснование (ЖК РФ, ФЗ, ПП РФ, ссылки на статьи)\n"
            "- Пошаговая инструкция:\n"
            "  * Рассрочка платежей (ЖК РФ, ст. 155.1)\n"
            "  * Подача претензий и обращений в УК, ГЖИ (ФЗ №59-ФЗ, ст. 12)\n"
            "  * Взыскание задолженности через суд (ГПК РФ, ст. 131)\n"
            "  * Исполнительное производство (ФЗ №229-ФЗ, ст. 69)\n"
            "  * Банкротство должника (ФЗ №127-ФЗ)\n"
        )
    
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчет пени (актуальная формула):**\n"
                "- Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Нормативная база: [ЖК РФ, ст. 155.1]\n"
                "- Ограничение: ≤ 9.5% годовых [ФЗ №44-ФЗ, ПП РФ №329]\n"
                "- Пример: долг 10 000 руб., просрочка 30 дней → Пени = 95 руб.\n"
                "- Начало начисления: с 31-го дня после срока оплаты.\n"
            )
    
        system_prompt += (
            "\n### Судебная практика:\n"
            "[**Определение ВС РФ №XXX-ЭСXX-XXXX от ДД.ММ.ГГГГ** — краткая позиция суда]\n"
            "Если судебных решений нет: 'Судебная практика по данному вопросу в базе отсутствует'.\n\n"
            "### Ключевые нормативные акты:\n"
            "- Жилищный кодекс РФ (ст. 155, 155.1, 158 — сроки оплаты, пени, взыскание)\n"
            "- ФЗ №229-ФЗ «Об исполнительном производстве»\n"
            "- ФЗ №230-ФЗ «О защите прав должников»\n"
            "- ФЗ №127-ФЗ «О банкротстве»\n"
            "- ГК РФ (ст. 196 — исковая давность)\n"
            "- ПП РФ №354 (раздел 8 — порядок расчётов)\n\n"
            f"{self.get_role_instruction(role)}"
        )
    
        return (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )
        
class IoTIntegrationAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Интеграция с IoT", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "умный счётчик": {
                "synonyms": ["интеллектуальный счётчик", "телеметрический счётчик", "счётчик с GSM", "автоматическая передача показаний"],
                "norm_refs": ["ФЗ №261-ФЗ, ст. 13(5)", "ПП РФ №354, п. 31"],
                "contexts": ["передача данных", "интеграция с ГИС ЖКХ", "тарифы", "замена", "поверка"]
            },
            "автоматическая передача": {
                "synonyms": ["дистанционная передача", "телеметрия", "беспроводная передача", "автоотправка", "API передачи"],
                "norm_refs": ["ФЗ №261-ФЗ", "ПП РФ №354, п. 31(1)"],
                "contexts": ["интервалы передачи", "надёжность", "ошибки передачи", "резервные каналы", "ручной ввод как дубль"]
            },
            "данные с датчика": {
                "synonyms": ["телеметрия", "показания датчиков", "сырые данные", "логи сенсоров", "пакеты данных"],
                "norm_refs": ["ФЗ №152-ФЗ", "ПП РФ №689"],
                "contexts": ["форматы (JSON, XML)", "частота опроса", "хранение", "аномалии", "визуализация"]
            },
            "аномальное потребление": {
                "synonyms": ["резкий скачок", "утечка воды", "короткое замыкание", "неисправность", "оповещение об аномалии"],
                "norm_refs": [],
                "contexts": ["алгоритмы обнаружения", "пороговые значения", "уведомления", "автоматическое отключение", "журнал событий"]
            },
            "утечка воды": {
                "synonyms": ["протечка", "затопление", "авария водоснабжения", "датчик протечки", "аварийное оповещение"],
                "norm_refs": [],
                "contexts": ["реагирование", "перекрытие крана", "уведомление в Telegram", "интеграция с УК", "акт аварии"]
            },
            "энергомониторинг": {
                "synonyms": ["мониторинг энергопотребления", "анализ нагрузки", "профили потребления", "снижение затрат", "умный дом"],
                "norm_refs": ["ФЗ №261-ФЗ", "ПП РФ №354"],
                "contexts": ["графики", "пики нагрузки", "тарифные зоны", "рекомендации по экономии", "интеграция с ИТП"]
            },
            "интеграция с приложением": {
                "synonyms": ["мобильное приложение", "личный кабинет", "веб-интерфейс", "дашборд", "виджеты"],
                "norm_refs": ["ФЗ №152-ФЗ", "ПП РФ №689"],
                "contexts": ["UX/UI", "push-уведомления", "аутентификация", "роль доступа", "экспорт данных"]
            },
            "MQTT": {
                "synonyms": ["протокол MQTT", "IoT протокол", "брокер MQTT", "publish/subscribe", "Mosquitto"],
                "norm_refs": [],
                "contexts": ["лёгкий протокол", "надёжность", "QoS уровни", "безопасность (TLS)", "интеграция с Home Assistant"]
            },
            "Zigbee": {
                "synonyms": ["радиопротокол Zigbee", "умный дом Zigbee", "Zigbee 3.0", "сенсоры Zigbee", "хаб Zigbee"],
                "norm_refs": [],
                "contexts": ["энергоэффективность", "сеть-ячеистая", "совместимость", "ограничения по расстоянию", "безопасность AES-128"]
            },
            "LoRaWAN": {
                "synonyms": ["дальний радиоканал", "LPWAN", "городская сеть", "датчики на улице", "умный город"],
                "norm_refs": [],
                "contexts": ["большой радиус", "низкое энергопотребление", "публичные сети", "государственная инфраструктура", "тарификация"]
            },
            "уведомления в телеграм": {
                "synonyms": ["Telegram-бот", "оповещения в WhatsApp", "push-уведомления", "SMS-оповещения", "email-рассылка"],
                "norm_refs": ["ФЗ №152-ФЗ, ст. 9", "ПП РФ №689"],
                "contexts": ["настройка", "согласие пользователя", "отказ от рассылки", "безопасность каналов", "шаблоны сообщений"]
            },
            "API для интеграции": {
                "synonyms": ["вебхуки", "REST API", "интерфейс интеграции", "документация API", "SDK", "GraphQL"],
                "norm_refs": ["ФЗ №149-ФЗ", "ФЗ №152-ФЗ"],
                "contexts": ["аутентификация (OAuth2, API-ключи)", "rate limiting", "логирование", "передача персональных данных", "HTTPS"]
            },
            "вебхуки": {
                "synonyms": ["webhook", "callback", "HTTP-уведомления", "асинхронные уведомления", "event-driven"],
                "norm_refs": ["ФЗ №149-ФЗ", "ФЗ №152-ФЗ"],
                "contexts": ["настройка URL", "подписи запросов (HMAC)", "обработка ошибок", "повторные попытки", "безопасность (HTTPS)"]
            },
            "безопасность данных": {
                "synonyms": ["защита информации", "шифрование", "GDPR", "персональные данные", "конфиденциальность", "аудит безопасности"],
                "norm_refs": ["ФЗ №152-ФЗ", "ПП РФ №689", "ФЗ №149-ФЗ"],
                "contexts": ["TLS/SSL", "аутентификация", "авторизация", "регулярные аудиты", "ответственность оператора"]
            },
            "перспективы развития": {
                "synonyms": ["будущее IoT", "цифровая трансформация ЖКХ", "искусственный интеллект", "предиктивная аналитика", "цифровой двойник"],
                "norm_refs": [],
                "contexts": ["госпрограммы", "гранты", "пилотные проекты", "стандартизация", "импортозамещение"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "digital.gov.ru", "roskomnadzor.ru", "fct.gov.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".roskomnadzor.ru", ".digital.gov.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ФЗ 152-ФЗ IoT ЖКХ")
        queries.append(f"{query} ПП РФ 689 персональные данные")
        queries.append(f"{query} умные счётчики интеграция API")
        queries.append(f"{query} уведомления в Telegram датчики протечки")
        queries.append(f"{query} MQTT Zigbee LoRaWAN сравнение")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Интеграция с IoT и цифровой мониторинг ЖКХ
        Формирует системный промт:
        - Фокус: IoT, цифровой мониторинг, интеграции, уведомления, нормативы по данным
        - Жёсткая структура и ссылки на нормативные акты
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        penalty_keywords = [
            "пени", "пеня", "неустойка", "штраф за просрочку",
            "ставка цб", "ключевая ставка", "расчет пени"
        ]
        should_calculate_penalty = any(kw in summary.lower() for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — эксперт по внедрению IoT и цифрового мониторинга в ЖКХ. "
            "Дай точный, структурированный и юридически корректный ответ, "
            "используя ТОЛЬКО контекст, веб-результаты и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если данных нет — отвечай: 'Недостаточно данных для точного ответа.'\n"
            "2. Структура ответа: Краткий вывод → Техническое решение → Нормативные требования → Рекомендации.\n"
            "3. Подкрепляй каждое утверждение ссылками на нормативные акты ([ФЗ №152-ФЗ, ст. 9], [ПП РФ №689, п. 4]).\n"
            "4. Формулы пени только при наличии ключевых слов.\n"
            "5. Приоритет источников: ФЗ > ПП РФ > технические стандарты.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "### Структура ответа:\n"
            "- Краткий вывод (1-2 предложения)\n"
            "- Техническое решение / Возможности (устройства, интеграция, уведомления)\n"
            "- Нормативные требования (обработка данных, согласие жильцов, меры безопасности)\n"
            "- Рекомендации по внедрению (этапы, юридические риски, примеры кейсов)\n"
        )
    
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчет пени (актуальная формула):**\n"
                "- Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Нормативная база: [ЖК РФ, ст. 155.1]\n"
                "- Ограничение: ≤ 9.5% годовых [ФЗ №44-ФЗ, ПП РФ №329]\n"
                "- Пример: долг 10 000 руб., просрочка 30 дней → Пени = 95 руб.\n"
                "- Начало начисления: с 31-го дня после срока оплаты.\n"
            )
    
        system_prompt += (
            "\n### Ключевые нормативные акты:\n"
            "- ФЗ №152-ФЗ «О персональных данных»\n"
            "- ПП РФ №689 «Об утверждении требований к защите персональных данных»\n"
            "- ФЗ №149-ФЗ «Об информации, ИТ и защите информации»\n"
            "- ФЗ №261-ФЗ (умные счётчики)\n"
            "- ПП РФ №354 (интеграция показаний счётчиков)\n\n"
            f"{self.get_role_instruction(role)}"
        )
    
        return (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )

        
class WasteManagementAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Вывоз ТКО", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "тко": {
                "synonyms": ["твердые коммунальные отходы", "бытовые отходы", "мусор", "вывоз мусора", "обращение с отходами"],
                "norm_refs": ["ФЗ №89-ФЗ", "ПП РФ №354, раздел 8"],
                "contexts": ["тариф", "норматив накопления", "расчёт", "перерасчёт", "объём"]
            },
            "региональный оператор": {
                "synonyms": ["ро", "оператор тко", "регоператор", "компания по вывозу мусора"],
                "norm_refs": ["ФЗ №89-ФЗ, ст. 24.7", "ПП РФ №1156"],
                "contexts": ["обязанности", "тарифы", "график вывоза", "жалобы", "ответственность"]
            },
            "контейнерная площадка": {
                "synonyms": ["мусорная площадка", "контейнеры", "переполненный контейнер", "санитарное состояние", "уборка площадки"],
                "norm_refs": ["ПП РФ №491, п. 12", "СанПиН 1.2.3685-21, п. 8.1"],
                "contexts": ["обязанность УК", "график уборки", "антисанитария", "крысы", "мухи", "дезинфекция"]
            },
            "перерасчёт за мусор": {
                "synonyms": ["перерасчет тко", "не вывозят мусор", "не оказана услуга", "акт об отсутствии вывоза", "заявление на перерасчёт"],
                "norm_refs": ["ПП РФ №354, п. 154", "ЖК РФ, ст. 157"],
                "contexts": ["сроки", "документы", "жалоба в УК/РО", "возврат средств", "судебная практика"]
            },
            "норматив накопления": {
                "synonyms": ["объём тко", "расчёт по нормативу", "кубометры на человека", "тариф по площади", "тариф по количеству проживающих"],
                "norm_refs": ["ПП РФ №354, п. 148", "ПП РФ №269"],
                "contexts": ["региональные различия", "сезонные коэффициенты", "изменение норматива", "расчёт платы"]
            },
            "раздельный сбор": {
                "synonyms": ["сортировка", "раздельный вывоз", "вторсырьё", "пластик", "стекло", "бумага", "металл"],
                "norm_refs": ["ФЗ №89-ФЗ, ст. 13.1", "ПП РФ №1342"],
                "contexts": ["обязательство с 2025 года", "цветные контейнеры", "экомаркировка", "пункты приёма", "ответственность"]
            },
            "антисанитария": {
                "synonyms": ["запах", "крысы", "мыши", "грызуны", "мухи", "дератизация", "дезинфекция", "санэпидемстанция", "сэс"],
                "norm_refs": ["СанПиН 1.2.3685-21, п. 8.1", "ФЗ №52-ФЗ"],
                "contexts": ["жалоба в Роспотребнадзор", "акт санитарной проверки", "предписание УК", "штрафы", "санитарно-эпидемиологическое заключение"]
            },
            "несанкционированная свалка": {
                "synonyms": ["незаконная свалка", "свалка", "навал", "скопление", "навалено", "пожароопасный", "опасный"],
                "norm_refs": ["ФЗ №89-ФЗ, ст. 8.1", "КоАП РФ, ст. 8.2"],
                "contexts": ["жалоба в Росприроднадзор", "фото/видео как доказательство", "привлечение виновных", "уборка за счёт бюджета", "административная ответственность"]
            },
            "опасные отходы": {
                "synonyms": ["батарейки", "лампочки", "энергосберегающие лампы", "ртутьсодержащие", "градусник", "термометр", "медицинские отходы"],
                "norm_refs": ["ФЗ №89-ФЗ, Приложение 1", "Постановление Правительства №712"],
                "contexts": ["класс опасности", "запрет захоронения", "специализированные пункты приёма", "ответственность за смешивание", "утилизация"]
            },
            "автошины": {
                "synonyms": ["покрышки", "резина", "колёса", "утилизация шин", "пункт приёма шин"],
                "norm_refs": ["ФЗ №89-ФЗ, Приложение 1", "Приказ Минприроды №779"],
                "contexts": ["не относится к ТКО", "захоронение запрещено", "ответственность владельца", "спецтехника для вывоза", "фронтальный погрузчик"]
            },
            "жалоба на мусор": {
                "synonyms": ["обращение по тко", "не убрали", "скопилось", "жалоба на регионального оператора", "жалоба на ук"],
                "norm_refs": ["ФЗ №59-ФЗ, ст. 12", "ПП РФ №493"],
                "contexts": ["образец жалобы", "срок ответа 30 дней", "жалоба в ГЖИ/Росприроднадзор", "внеплановая проверка", "предписание"]
            },
            "график вывоза": {
                "synonyms": ["расписание вывоза", "частота вывоза", "ежедневно", "через день", "по графику"],
                "norm_refs": ["ФЗ №89-ФЗ, ст. 24.7", "ПП РФ №1156"],
                "contexts": ["обязательное соблюдение", "информирование жильцов", "ответственность РО", "штрафы за нарушение"]
            },
            "тариф на вывоз": {
                "synonyms": ["тариф тко", "расчет за тко", "плата за мусор", "норматив + тариф", "объём × тариф"],
                "norm_refs": ["ПП РФ №354, п. 148", "ФЗ №210-ФЗ"],
                "contexts": ["расчёт по площади/по количеству", "региональный тариф", "обоснование", "жалоба в ФАС", "ФГИС ТКО"]
            },
            "пункт приема": {
                "synonyms": ["пункт приёма", "экопункт", "центр приёма вторсырья", "пункт утилизации", "пункт сбора опасных отходов"],
                "norm_refs": ["ФЗ №89-ФЗ, ст. 13.1", "ПП РФ №1342"],
                "contexts": ["адреса", "график работы", "принимаемые отходы", "бесплатная сдача", "экологические бонусы"]
            },
            "смешивание отходов": {
                "synonyms": ["смешивать отходы", "батарейки с мусором", "ртуть в контейнере", "опасные с бытовыми", "нарушение сортировки"],
                "norm_refs": ["ФЗ №89-ФЗ, ст. 13.1", "КоАП РФ, ст. 8.2"],
                "contexts": ["административная ответственность", "запрет", "раздельный сбор", "штрафы", "экологический ущерб"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "rpn.gov.ru", "mnr.gov.ru", "rosconsumnadzor.ru", "vsrf.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".rpn.gov.ru", ".vsrf.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ФЗ 89-ФЗ ТКО")
        queries.append(f"{query} ПП РФ 354 раздел 8")
        queries.append(f"{query} судебная практика по перерасчету за ТКО")
        queries.append(f"{query} класс опасности батареек лампочек")
        queries.append(f"{query} куда сдать автошины покрышки")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Вывоз ТКО
        Формирует системный промт:
        - Фокус: классификация отходов, запрет смешивания, порядок утилизации, расчёт платы, нормативы
        - Жёсткая структура и ссылки на нормативные акты
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        penalty_keywords = ["пени", "неустойка", "штраф за просрочку", "ставка цб", "9.5%", "ключевая ставка"]
        q_lower = summary.lower()
        should_calculate_penalty = any(kw in q_lower for kw in penalty_keywords)
    
        hazardous_keywords = [
            "автошины", "покрышки", "резина", "батарейки", "лампочки",
            "энергосберегающие лампы", "ртутьсодержащие", "градусник",
            "термометр", "медицинские отходы", "ртуть", "кислота",
            "краска", "лак", "масло", "строительный мусор", "техника", "мебель"
        ]
        mentions_hazardous = any(kw in q_lower for kw in hazardous_keywords)
    
        mixing_keywords = [
            "смешивание", "смешивать", "батарейки с мусором", "ртуть в контейнере",
            "опасные с бытовыми", "нарушение сортировки", "вместе с тко"
        ]
        mentions_mixing = any(kw in q_lower for kw in mixing_keywords)
    
        system_prompt = (
            "Ты — эксперт по обращению с твёрдыми коммунальными отходами (ТКО) в ЖКХ. "
            "Отвечай строго по нормативам, без выдуманных данных, используя ТОЛЬКО предоставленный контекст и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если данных нет — отвечай: 'Недостаточно данных для точного ответа. Обратитесь в УК.'\n"
            "2. Указывай ссылки на нормативные акты (ФЗ, ПП РФ, СанПиН, КоАП).\n"
            "3. Структура: краткий вывод → нормативы → классификация отходов → запрет смешивания → порядок утилизации → перерасчёты → судебная практика.\n"
            "4. Формулы пени включай только при упоминании пени или штрафов.\n"
            "5. Приоритет региональных актов над федеральными.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
        )
    
        if mentions_hazardous:
            system_prompt += (
                "--- Классификация отходов ---\n"
                "1. Класс опасности: [указать из контекста или 'не указан']\n"
                "2. Относится ли к ТКО: [Да/Нет]\n"
                "3. Разрешено захоронение: [Да/Нет, если нет — ФЗ №89-ФЗ, ст.12]\n"
                "4. Ответственный за утилизацию: [Собственник/Гражданин]\n"
                "5. Способ утилизации: [пункты приёма, спецтехника]\n\n"
            )
    
        if mentions_mixing:
            system_prompt += (
                "--- Запрет смешивания ---\n"
                "Смешивание отходов разных классов опасности строго запрещено ФЗ №89-ФЗ, ст. 13.1.\n"
                "Нарушение влечет ответственность по ст. 8.2 КоАП РФ.\n\n"
            )
    
        system_prompt += (
            "--- Основной ответ ---\n"
            "Краткий вывод: [1-2 предложения — что делать, куда обращаться]\n"
            "Нормативное обоснование: [ФЗ №89-ФЗ, ПП РФ, СанПиН]\n"
            "Пошаговая инструкция: [расчёт платы, перерасчёт, ответственные лица, утилизация]\n"
            "Судебная практика: [если есть, указать; иначе 'отсутствует']\n\n"
            "### Ключевые нормативные акты:\n"
            "- ФЗ №89-ФЗ «Об отходах производства и потребления»\n"
            "- ПП РФ №354 (расчёт платы за ТКО)\n"
            "- ПП РФ №491 (контейнерные площадки)\n"
            "- СанПиН 1.2.3685-21\n"
            "- КоАП РФ, ст. 8.2\n"
        )
    
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчёт пени (если упомянут):**\n"
                "- Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Основание: [ЖК РФ, ст.155.1], [ФЗ №44-ФЗ], [ПП РФ №329]\n"
                "- Ограничение: ≤ 9.5% годовых\n"
                "- Пример: 10 000 руб., просрочка 30 дней → 95 руб.\n"
                "- Начало начисления: с 31-го дня после срока оплаты\n"
            )
    
        system_prompt += f"{self.get_role_instruction(role)}"
    
        return (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )

class AccountManagementAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Управление лицевыми счетами", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "лицевой счет": {
                "synonyms": ["лицевой счёт", "единый лицевой счет", "едлс", "расчётный счёт жильца", "номер лицевого счёта"],
                "norm_refs": ["ЖК РФ, ст. 154", "ПП РФ №354, п. 93"],
                "contexts": ["открытие", "закрытие", "объединение", "разделение", "переоформление", "реквизиты"]
            },
            "объединить счета": {
                "synonyms": ["объединение лицевых счетов", "слияние счетов", "единый счёт на квартиру", "один счёт на всех собственников"],
                "norm_refs": ["ЖК РФ, ст. 154", "ПП РФ №354, п. 93(4)"],
                "contexts": ["по заявлению собственников", "договор управления", "техническая возможность", "журнал регистрации"]
            },
            "разделить счет": {
                "synonyms": ["разделение лицевого счёта", "выделение доли", "отдельный счёт на долю", "индивидуальный платёжный документ"],
                "norm_refs": ["ЖК РФ, ст. 154(2)", "ПП РФ №354, п. 94(2)"],
                "contexts": ["на основании соглашения", "судебное решение", "техническая возможность", "ограничения по коммунальным услугам"]
            },
            "переоформить счет": {
                "synonyms": ["смена собственника", "перерегистрация счёта", "передача лицевого счёта", "вступление в наследство"],
                "norm_refs": ["ЖК РФ, ст. 154", "ПП РФ №354, п. 93(3)"],
                "contexts": ["документы о праве собственности", "акт приёма-передачи", "заявление нового собственника", "сроки переоформления"]
            },
            "доверенность": {
                "synonyms": ["по доверенности", "нотариальная доверенность", "генеральная доверенность", "представительство", "доверенное лицо"],
                "norm_refs": ["ГК РФ, ст. 185", "ЖК РФ, ст. 154"],
                "contexts": ["права представителя", "срок действия", "образец доверенности", "регистрация в УК", "отмена доверенности"]
            },
            "собственник": {
                "synonyms": ["не собственник", "владелец", "правообладатель", "арендатор", "наниматель"],
                "norm_refs": ["ЖК РФ, ст. 153", "ФЗ №218-ФЗ"],
                "contexts": ["обязанности по оплате", "право на управление счётом", "предоставление документов", "регистрация права"]
            },
            "правоустанавливающие документы": {
                "synonyms": ["выписка егрн", "договор купли-продажи", "дарственная", "наследство", "технический паспорт", "кадастровый паспорт"],
                "norm_refs": ["ФЗ №218-ФЗ", "ЖК РФ, ст. 154"],
                "contexts": ["для открытия/переоформления счёта", "подтверждение права", "госрегистрация", "архивные справки"]
            },
            "регистрация права": {
                "synonyms": ["регистрация по месту жительства", "прописка", "временная регистрация", "постоянная регистрация", "паспортный стол"],
                "norm_refs": ["ФЗ №5242-1", "ПП РФ №713"],
                "contexts": ["влияние на расчёт по нормативу", "изменение состава семьи", "документы для регистрации", "сроки регистрации"]
            },
            "открыть счет": {
                "synonyms": ["создание лицевого счёта", "инициализация счёта", "первичная регистрация", "постановка на учёт"],
                "norm_refs": ["ЖК РФ, ст. 154", "ПП РФ №354, п. 93(1)"],
                "contexts": ["при заселении новостройки", "после приватизации", "при первичной регистрации права", "документы для открытия"]
            },
            "закрыть счет": {
                "synonyms": ["аннулирование лицевого счёта", "прекращение учёта", "ликвидация счёта", "счёт закрыт"],
                "norm_refs": ["ЖК РФ, ст. 154", "ПП РФ №354, п. 93(5)"],
                "contexts": ["при сносе дома", "при объединении счётов", "при ликвидации объекта", "погашение задолженности", "архивация"]
            },
            "изменение состава семьи": {
                "synonyms": ["рождение ребёнка", "смерть", "развод", "брак", "выписка/прописка", "временная регистрация"],
                "norm_refs": ["ПП РФ №354, п. 93(3)", "ЖК РФ, ст. 154"],
                "contexts": ["перерасчёт по нормативу", "обновление данных в ГИС ЖКХ", "заявление в УК", "сроки уведомления (5 дней)"]
            },
            "документы для регистрации": {
                "synonyms": ["оформить прописку", "где оформить регистрацию", "паспорт", "заявление по форме №6", "документы собственника"],
                "norm_refs": ["ПП РФ №713", "ФЗ №5242-1"],
                "contexts": ["МФЦ", "Госуслуги", "паспортный стол", "срок оформления (3-8 дней)", "штрафы за нарушение сроков"]
            },
            "передача прав": {
                "synonyms": ["дарение квартиры", "купля-продажа", "наследство", "рента", "мена", "передача по договору"],
                "norm_refs": ["ГК РФ, гл. 30-33", "ФЗ №218-ФЗ"],
                "contexts": ["реестровая запись", "акт приёма-передачи", "уведомление УК", "переоформление лицевого счёта", "долги нового собственника"]
            },
            "выписка из ЕГРН": {
                "synonyms": ["выписка егрн", "свидетельство о праве", "документ о собственности", "онлайн выписка", "архивная выписка"],
                "norm_refs": ["ФЗ №218-ФЗ, ст. 62", "ПП РФ №753"],
                "contexts": ["для УК", "для суда", "для нотариуса", "срок действия", "электронная подпись", "получение через Госуслуги"]
            },
            "технический паспорт": {
                "synonyms": ["кадастровый паспорт", "техплан", "экспликация", "поэтажный план", "БТИ"],
                "norm_refs": ["ФЗ №221-ФЗ", "ПП РФ №1463"],
                "contexts": ["для разделения счёта", "для перепланировки", "для суда", "для нотариуса", "срок действия", "обновление при изменении"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "rosreestr.gov.ru", "gosuslugi.ru", "мфц.рф", "vsrf.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".rosreestr.gov.ru", ".gosuslugi.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ЖК РФ ст 154 лицевой счет")
        queries.append(f"{query} ПП РФ 354 раздел 9")
        queries.append(f"{query} как разделить лицевой счет судебная практика")
        queries.append(f"{query} документы для переоформления лицевого счета")
        queries.append(f"{query} доверенность на управление лицевым счетом ЖКХ")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Управление лицевыми счетами ЖКХ
        Формирует системный промт:
        - Фокус: открытие, закрытие, разделение, доверенности, смена собственника
        - Жёсткая структура с ссылками на нормативные акты
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        # Проверка на необходимость расчета пени
        penalty_keywords = ["пени", "неустойка", "штраф за просрочку", "ставка цб", "9.5%", "ключевая ставка"]
        q_lower = summary.lower()
        should_calculate_penalty = any(kw in q_lower for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — эксперт по управлению лицевыми счетами в сфере ЖКХ. "
            "Отвечай строго по нормативам, без выдуманных данных, используя только контекст и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если данных нет — отвечай: 'Недостаточно данных для точного ответа. Обратитесь в УК.'\n"
            "2. Указывай ссылки на нормативные акты (ЖК РФ, ПП РФ, ФЗ).\n"
            "3. Структура: краткий вывод → нормативы → пошаговая инструкция → доверенности → смена собственника → судебная практика.\n"
            "4. Формулы пени включай только при упоминании пени.\n"
            "5. Приоритет региональных актов над федеральными.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "--- Основной ответ ---\n"
            "Краткий вывод: [1-2 предложения — что делать, какие документы нужны, куда обращаться]\n"
            "Нормативное обоснование: [ЖК РФ, ПП РФ, ФЗ]\n"
            "Пошаговая инструкция:\n"
            "- Открытие/закрытие/переоформление лицевого счета (документы, сроки — ЖК РФ, ст.154; ПП РФ №354, п.93)\n"
            "- Разделение/объединение счёта (соглашение, техническая возможность, судебное решение — ПП РФ №354, п.94)\n"
            "- Оформление доверенности (нотариальная форма, регистрация в УК — ГК РФ, ст.185)\n"
            "- Изменение собственника или состава семьи (уведомление в 5 дней — ПП РФ №354, п.93(3))\n"
            "- Получение выписки ЕГРН или техпаспорта (Росреестр, МФЦ — ФЗ №218-ФЗ)\n\n"
            "Судебная практика: [если есть, указать; иначе 'отсутствует']\n"
            "### Ключевые нормативные акты:\n"
            "- Жилищный кодекс РФ (ст.153-155)\n"
            "- ПП РФ №354 (п.93-94)\n"
            "- ФЗ №218-ФЗ «О государственной регистрации недвижимости»\n"
            "- ГК РФ, ст.185 (доверенность)\n"
        )
    
        # --- Динамический блок: расчет пени ---
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчёт пени (если упомянут):**\n"
                "- Формула: Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Основание: [ЖК РФ, ст.155.1], [ФЗ №44-ФЗ], [ПП РФ №329]\n"
                "- Ограничение: не более 9.5% годовых до 2027 года\n"
                "- Пример: 10 000 руб., просрочка 30 дней → 95 руб.\n"
                "- Начало начисления: с 31-го дня после окончания срока оплаты\n"
            )
    
        system_prompt += f"{self.get_role_instruction(role)}"
    
        # --- Формат для Saiga/LLaMA-3 ---
        prompt_formatted = (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )
    
        return prompt_formatted
        
class ContractAndMeetingAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Договоры и решения ОСС", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "договор управления": {
                "synonyms": ["контракт", "соглашение", "договор с ук", "расторгнуть договор", "расторжение", "заключен", "подписан"],
                "norm_refs": ["ЖК РФ, ст. 161-162", "ПП РФ №416"],
                "contexts": ["существенные условия", "срок действия", "прекращение", "односторонний отказ", "судебное расторжение"]
            },
            "общее собрание собственников": {
                "synonyms": ["осс", "собрание", "голосование", "решение собрания", "решение осс", "протокол осс", "инициатор собрания"],
                "norm_refs": ["ЖК РФ, ст. 44-48", "ПП РФ №416"],
                "contexts": ["очная/заочная форма", "электронное голосование", "кворум", "повестка", "уведомление собственников"]
            },
            "реклама в доме": {
                "synonyms": ["реклама в лифте", "реклама в подъезде", "рекламная компания", "размещение рекламы", "доход от рекламы"],
                "norm_refs": ["ЖК РФ, ст. 36", "ГК РФ, ст. 672"],
                "contexts": ["требуется решение ОСС", "целевое использование доходов", "договор аренды", "запрет без согласия"]
            },
            "земельный участок": {
                "synonyms": ["придомовая территория", "земля под домом", "аренда земельного участка", "использование земли"],
                "norm_refs": ["ЖК РФ, ст. 36", "ЗК РФ, ст. 39.20"],
                "contexts": ["право собственности", "аренда с доходом", "благоустройство", "решение ОСС", "госрегистрация"]
            },
            "проверить договор": {
                "synonyms": ["статус договора", "юридическая сила", "недействительный договор", "оспаривание договора", "экспертиза договора"],
                "norm_refs": ["ГК РФ, ст. 168", "ЖК РФ, ст. 162"],
                "contexts": ["соответствие ЖК РФ", "существенные условия", "регистрация", "жалоба в ГЖИ", "судебная экспертиза"]
            },
            "нецелевое использование": {
                "synonyms": ["списала деньги", "собранные средства", "нарушение решения осс", "целевые средства", "компенсация долгов"],
                "norm_refs": ["ЖК РФ, ст. 161.1", "ГК РФ, ст. 1102"],
                "contexts": ["доходы от рекламы", "средства капремонта", "штрафы", "взыскание через суд", "ревизия счетов"]
            },
            "приемка работ": {
                "synonyms": ["принимал работы", "акт приемки", "некачественный ремонт", "испортили имущество", "восстановить дверь", "кто виноват"],
                "norm_refs": ["ЖК РФ, ст. 162", "ГК РФ, ст. 723, 753"],
                "contexts": ["состав комиссии", "срок подписания", "односторонний акт", "экспертиза", "регресс к подрядчику"]
            },
            "ответственность подрядчика": {
                "synonyms": ["некачественный ремонт", "испортили имущество", "восстановить", "возместить ущерб", "гарантийный срок", "претензия"],
                "norm_refs": ["ГК РФ, ст. 723", "ЖК РФ, ст. 162"],
                "contexts": ["акт скрытых работ", "независимая экспертиза", "взыскание убытков", "моральный вред", "судебный иск"]
            },
            "решение осс": {
                "synonyms": ["решение собрания", "протокол осс", "голосование", "итоги голосования", "недействительное решение", "оспаривание решения"],
                "norm_refs": ["ЖК РФ, ст. 46", "ПП РФ №416, п. 21"],
                "contexts": ["обязательность для всех", "срок оспаривания (6 месяцев)", "основания для признания недействительным", "жалоба в ГЖИ"]
            },
            "аренда общего имущества": {
                "synonyms": ["реклама", "установка банкомата", "аренда подвала", "сдача в аренду", "доходная статья"],
                "norm_refs": ["ЖК РФ, ст. 36(4)", "ГК РФ, ст. 672"],
                "contexts": ["решение ОСС обязательно", "договор аренды", "целевое использование доходов", "отчёт перед собственниками"]
            },
            "расторжение договора": {
                "synonyms": ["прекращение договора", "отказ от услуг", "смена ук", "односторонний отказ", "в одностороннем порядке"],
                "norm_refs": ["ЖК РФ, ст. 162(8)", "ГК РФ, ст. 450"],
                "contexts": ["по инициативе собственников", "по решению ОСС", "по вине УК", "уведомление за 30 дней", "передача документации"]
            },
            "юридическая сила": {
                "synonyms": ["недействительный договор", "ничтожная сделка", "оспоримая сделка", "признание недействительным", "недействительность решения осс"],
                "norm_refs": ["ГК РФ, ст. 166-181", "ЖК РФ, ст. 46(5)"],
                "contexts": ["нарушение порядка", "отсутствие кворума", "неправомочность УК", "судебное оспаривание", "последствия признания"]
            },
            "целевые средства": {
                "synonyms": ["доходы от рекламы", "средства от аренды", "средства капремонта", "спецсчёт", "назначение платежа"],
                "norm_refs": ["ЖК РФ, ст. 161.1", "ПП РФ №416, п. 22"],
                "contexts": ["запрет на компенсацию долгов", "отчётность", "ревизия", "ответственность УК", "взыскание через суд"]
            },
            "голосование": {
                "synonyms": ["электронное голосование", "голосование через ГИС ЖКХ", "заочное голосование", "очное голосование", "кворум", "большинство голосов"],
                "norm_refs": ["ЖК РФ, ст. 47", "ПП РФ №416, п. 12"],
                "contexts": ["2/3 голосов", "50%+1", "расчёт по долям", "идентификация голосующих", "итоги голосования"]
            },
            "акт приемки": {
                "synonyms": ["акт выполненных работ", "приёмка-передача", "подписать акт", "не подписан", "замечания", "односторонний акт"],
                "norm_refs": ["ГК РФ, ст. 753", "ПП РФ №416, п. 20"],
                "contexts": ["обязательные реквизиты", "срок подписания (5 дней)", "фото/видео как доказательство", "использование в суде"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "dom.gosuslugi.ru", "gjirf.ru", "vsrf.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".gosuslugi.ru", ".vsrf.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ЖК РФ ст 161 договор управления")
        queries.append(f"{query} ПП РФ 416 общее собрание")
        queries.append(f"{query} судебная практика по расторжению договора с УК")
        queries.append(f"{query} ответственность подрядчика за некачественный ремонт")
        queries.append(f"{query} можно ли использовать доходы от рекламы на погашение долгов")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Договоры управления и решения ОСС
        Формирует системный промт:
        - Фокус: заключение и расторжение договоров, проведение ОСС, ответственность, реклама
        - Жёсткая структура с ссылками на нормативные акты
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        # Проверка на необходимость расчета пени
        penalty_keywords = ["пени", "неустойка", "штраф за просрочку", "ставка цб", "9.5%", "ключевая ставка"]
        q_lower = summary.lower()
        should_calculate_penalty = any(kw in q_lower for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — эксперт по договорам управления и решениям общих собраний собственников в сфере ЖКХ. "
            "Отвечай строго по нормативам, без выдуманных данных, используя только контекст и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если данных нет — отвечай: 'Недостаточно данных для точного ответа. Обратитесь в УК.'\n"
            "2. Указывай ссылки на нормативные акты (ЖК РФ, ГК РФ, ПП РФ).\n"
            "3. Структура: краткий вывод → нормативы → пошаговая инструкция → ответственность → судебная практика.\n"
            "4. Формулы пени включай только при упоминании пени.\n"
            "5. Приоритет региональных актов над федеральными.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "--- Основной ответ ---\n"
            "Краткий вывод: [1-2 предложения — законно ли действие, что делать, куда обращаться]\n"
            "Нормативное обоснование: [ЖК РФ, ГК РФ, ПП РФ]\n"
            "Пошаговая инструкция:\n"
            "- Заключение/расторжение договора управления (решение ОСС, уведомление — ЖК РФ, ст.161-162)\n"
            "- Проведение ОСС и оформление протокола (уведомление, кворум, подписание — ПП РФ №416)\n"
            "- Размещение рекламы (только с решением ОСС — ЖК РФ, ст.36)\n"
            "- Действия при некачественном ремонте (акт, претензия, экспертиза — ГК РФ, ст.723)\n"
            "- Жалобы на нарушение решений ОСС (ГЖИ, прокуратура, суд — ЖК РФ, ст.20)\n\n"
            "Судебная практика: [если есть, указать; иначе 'отсутствует']\n"
            "### Ключевые нормативные акты:\n"
            "- Жилищный кодекс РФ (Глава 6 — ОСС, ст.161-162 — договор управления)\n"
            "- ПП РФ №416 «О порядке проведения общего собрания...»\n"
            "- Гражданский кодекс РФ (ст.168 — недействительность сделок, ст.723 — ответственность подрядчика)\n"
            "- Земельный кодекс РФ (ст.39.20 — аренда земельных участков под МКД)\n"
        )
    
        # --- Динамический блок: расчет пени ---
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчёт пени (если упомянут):**\n"
                "- Формула: Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Основание: [ЖК РФ, ст.155.1], [ФЗ №44-ФЗ], [ПП РФ №329]\n"
                "- Ограничение: не более 9.5% годовых до 2027 года\n"
                "- Пример: 10 000 руб., просрочка 30 дней → 95 руб.\n"
                "- Начало начисления: с 31-го дня после окончания срока оплаты\n"
            )
    
        system_prompt += f"{self.get_role_instruction(role)}"
    
        # --- Формат для Saiga/LLaMA-3 ---
        prompt_formatted = (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )
    
        return prompt_formatted
        
class RegionalMunicipalAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Региональные и муниципальные акты", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "региональный закон": {
                "synonyms": ["закон субъекта", "закон [область/край/республика]", "нормативный акт региона", "региональное законодательство"],
                "norm_refs": ["ЖК РФ, ст. 158.1", "ФЗ №131-ФЗ, ст. 26"],
                "contexts": ["тарифы", "льготы", "программы капремонта", "нормативы потребления", "социальные нормы"]
            },
            "муниципальный акт": {
                "synonyms": ["постановление мэрии", "распоряжение главы", "акт местного самоуправления", "постановление [название города]", "решение городской думы"],
                "norm_refs": ["ЖК РФ, ст. 155", "ФЗ №131-ФЗ, ст. 14"],
                "contexts": ["тарифы на услуги", "сроки оплаты", "льготы", "благоустройство", "содержание территории"]
            },
            "тариф в регионе": {
                "synonyms": ["тариф на отопление в москве", "тариф на воду в спб", "тариф на тко в екатеринбурге", "тариф на электроэнергию в новосибирске"],
                "norm_refs": ["ФЗ №210-ФЗ", "ПП РФ №1149"],
                "contexts": ["утверждение РТС", "обоснование", "жалоба в ФАС", "расчёт", "ФГИС Тариф"]
            },
            "норматив в городе": {
                "synonyms": ["норматив по тко в спб", "норматив потребления воды в москве", "норматив отопления в казани", "норматив на электроэнергию в нижнем новгороде"],
                "norm_refs": ["ПП РФ №306", "ПП РФ №354, п. 42"],
                "contexts": ["утверждение региона", "сезонные коэффициенты", "дифференцированные нормативы", "расчёт по нормативу", "жалоба на завышение"]
            },
            "программа капремонта": {
                "synonyms": ["программа капремонта [регион]", "график ремонта по региону", "перечень домов на капремонт", "сроки капремонта в [городе]"],
                "norm_refs": ["ЖК РФ, ст. 168", "ФЗ №271-ФЗ"],
                "contexts": ["утверждение субъектом РФ", "корректировка", "перенос сроков", "финансирование", "отчётность"]
            },
            "льготы в субъекте": {
                "synonyms": ["региональные льготы", "дополнительные льготы в [регионе]", "компенсации в [городе]", "субсидии в [области]"],
                "norm_refs": ["ЖК РФ, ст. 159", "Указ Президента №431"],
                "contexts": ["многодетные", "ветераны труда региона", "молодые семьи", "льготы по нормативу", "доплаты к федеральным льготам"]
            },
            "местные правила": {
                "synonyms": ["муниципальные нормы", "правила благоустройства", "правила содержания территории", "местные требования"],
                "norm_refs": ["ФЗ №131-ФЗ, ст. 14", "ЖК РФ, ст. 161"],
                "contexts": ["уборка дворов", "содержание контейнерных площадок", "озеленение", "парковка", "ответственность УК"]
            },
            "распоряжение губернатора": {
                "synonyms": ["указ губернатора", "постановление правительства региона", "приказ министерства региона", "распоряжение главы субъекта"],
                "norm_refs": ["ФЗ №131-ФЗ", "ЖК РФ, ст. 158.1"],
                "contexts": ["введение режима ЧС", "изменение тарифов", "льготы", "программы поддержки", "экстренные меры"]
            },
            "фгис тариф": {
                "synonyms": ["федеральная государственная информационная система тариф", "тарифы онлайн", "официальный тариф", "реестр тарифов"],
                "norm_refs": ["ФЗ №210-ФЗ", "ПП РФ №1149"],
                "contexts": ["публичный доступ", "поиск по региону", "скачивание документов", "обжалование тарифов", "история изменений"]
            },
            "ртс": {
                "synonyms": ["региональная тарифная служба", "тарифный комитет", "департамент цен и тарифов", "служба по тарифам [региона]"],
                "norm_refs": ["ФЗ №210-ФЗ", "ПП РФ №1149"],
                "contexts": ["утверждение тарифов", "обоснование", "публичные слушания", "жалобы", "ответственность"]
            },
            "социальные нормы": {
                "synonyms": ["нормативы для льготников", "льготные нормативы", "нормы потребления для малоимущих", "социальные объёмы"],
                "norm_refs": ["ЖК РФ, ст. 157", "региональные законы"],
                "contexts": ["расчёт субсидий", "льготы", "тарифы", "расчёт по нормативу", "документы для подтверждения"]
            },
            "публичные слушания": {
                "synonyms": ["обсуждение тарифов", "слушания по нормативам", "приём граждан", "консультации по ЖКХ"],
                "norm_refs": ["ФЗ №210-ФЗ, ст. 10", "ФЗ №131-ФЗ"],
                "contexts": ["обязательность", "сроки", "участие жильцов", "протоколы", "влияние на решения"]
            },
            "жалоба на региональный акт": {
                "synonyms": ["оспаривание постановления", "обжалование тарифа", "жалоба в прокуратуру", "исковое заявление", "надзор прокурора"],
                "norm_refs": ["ФЗ №59-ФЗ", "ГПК РФ, ст. 254"],
                "contexts": ["срок 3 месяца", "приложение документов", "рассмотрение судом", "приоставление действия акта", "вступление в дело прокурора"]
            },
            "сайт регионального органа": {
                "synonyms": ["официальный сайт [регион]", "портал [область].рф", "тарифы [город].рф", "жкх [край].рф"],
                "norm_refs": [],
                "contexts": ["доступ к документам", "поиск нормативов", "скачивание форм", "электронные обращения", "актуальные тарифы"]
            },
            "судебная практика по региональным актам": {
                "synonyms": ["оспаривание тарифа в суде", "признание акта недействительным", "обжалование постановления мэрии", "позиция ВС РФ"],
                "norm_refs": ["ГПК РФ, ст. 254", "КоАП РФ, ст. 30.17"],
                "contexts": ["основания для оспаривания", "сроки", "доказательства", "роль прокурора", "последствия признания недействительным"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "regulation.gov.ru", "vsrf.ru", "fstrf.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".fgis-tarif.ru", ".vsrf.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        # Извлекаем название региона из запроса
        region_keywords = [
            "москва", "московская область", "санкт-петербург", "спб", "екатеринбург", "казань",
            "новосибирск", "нижний новгород", "самара", "ростов-на-дону", "челябинск", "омск"
        ]
        detected_region = None
        for region in region_keywords:
            if region in query.lower():
                detected_region = region
                break

        if detected_region:
            queries.append(f"{query} официальный сайт {detected_region}")
            queries.append(f"{query} постановление правительства {detected_region}")
            queries.append(f"{query} тарифы {detected_region} ФГИС Тариф")
        else:
            queries.append(f"{query} региональный закон ЖКХ")
            queries.append(f"{query} муниципальный акт ЖКХ")

        queries.append(f"{query} судебная практика по оспариванию региональных актов")
        queries.append(f"{query} как найти официальный текст постановления мэрии")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Региональные и муниципальные акты ЖКХ
        Формирует системный промт:
        - Фокус: поиск, актуальность, обжалование региональных/муниципальных актов
        - Жёсткая структура с ссылками на нормативные акты
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        # Проверка на необходимость расчета пени
        penalty_keywords = ["пени", "неустойка", "штраф за просрочку", "ставка цб", "9.5%", "ключевая ставка"]
        q_lower = summary.lower()
        should_calculate_penalty = any(kw in q_lower for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — эксперт по региональным и муниципальным актам в сфере ЖКХ. "
            "Отвечай строго по нормативам, без выдуманных данных, используя только контекст и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если информации нет — отвечай: 'Недостаточно данных для точного ответа. Обратитесь в вашу УК.'\n"
            "2. Указывай ссылки на нормативные акты (региональные, муниципальные, федеральные).\n"
            "3. Структура: краткий вывод → нормативы → пошаговая инструкция → судебная практика.\n"
            "4. Формулы пени включай только при упоминании пени.\n"
            "5. Приоритет региональных/муниципальных актов над федеральными, если вопрос региональный.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "--- Основной ответ ---\n"
            "Краткий вывод: [1-2 предложения — действует ли акт, где найти, законно ли начисление]\n"
            "Нормативное обоснование: [точные названия, номера, даты региональных/муниципальных и федеральных актов]\n"
            "Пошаговая инструкция:\n"
            "- Где найти официальный текст акта (сайт региона, ФГИС Тариф, портал госуслуг)\n"
            "- Как проверить актуальность (дата вступления, изменения, отменяющие акты)\n"
            "- Как обжаловать акт (жалоба в вышестоящий орган, прокуратуру, суд — ФЗ №59-ФЗ, ГПК РФ, ст.254)\n"
            "- Как применить акт на практике (расчёт тарифа, оформление льготы, участие в капремонте)\n\n"
            "Судебная практика: [если есть, указать; иначе 'отсутствует']\n"
            "### Ключевые нормативные акты:\n"
            "- Жилищный кодекс РФ (ст.155, ст.158.1, гл.7 — местное самоуправление)\n"
            "- ФЗ №131-ФЗ «Об общих принципах организации местного самоуправления»\n"
            "- ФЗ №210-ФЗ «Об основах государственного регулирования тарифов»\n"
            "- ПП РФ №354, №491 — федеральные правила, если региональные не установлены\n"
            "- ПП РФ №1149 — о ФГИС Тариф\n"
        )
    
        # --- Динамический блок: расчет пени ---
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчёт пени (если упомянут):**\n"
                "- Формула: Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Основание: [ЖК РФ, ст.155.1], [ФЗ №44-ФЗ], [ПП РФ №329]\n"
                "- Ограничение: не более 9.5% годовых до 2027 года\n"
                "- Пример: 10 000 руб., просрочка 30 дней → 95 руб.\n"
                "- Начало начисления: с 31-го дня после окончания срока оплаты\n"
            )
    
        system_prompt += f"{self.get_role_instruction(role)}"
    
        # --- Формат для QVikhr / LLaMA-3 ---
        prompt_formatted = (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )
    
        return prompt_formatted
        
class CourtPracticeAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Судебная практика и разъяснения", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "судебная практика": {
                "synonyms": ["арбитражная практика", "практика судов", "судебные прецеденты", "позиция судов", "как суды трактуют"],
                "norm_refs": [],
                "contexts": ["жилищные споры", "взыскание долгов", "качество услуг", "оспаривание начислений", "ответственность УК"]
            },
            "определение вс рф": {
                "synonyms": ["решение верховного суда", "определение ВС РФ", "судебный акт ВС РФ", "позиция ВС РФ"],
                "norm_refs": [],
                "contexts": ["индекс дела", "дата вынесения", "краткая позиция", "значение для нижестоящих судов", "цитирование в исках"]
            },
            "постановление пленума вс рф": {
                "synonyms": ["разъяснения вс рф", "постановление пленума", "обзор практики пленума", "разъяснения высшей инстанции"],
                "norm_refs": [],
                "contexts": ["обязательны для судов", "толкование норм", "единая практика", "применение в спорах", "ссылка в судебных актах"]
            },
            "обзор судебной практики": {
                "synonyms": ["обзор практики", "анализ решений", "статистика судов", "обобщение практики", "рекомендации судам"],
                "norm_refs": [],
                "contexts": ["ВС РФ", "кассационные суды", "арбитражные суды", "по конкретным категориям дел", "по итогам года/квартала"]
            },
            "разъяснение минстроя": {
                "synonyms": ["письмо минстроя", "разъяснения министерства строительства", "методические рекомендации", "ответы на вопросы"],
                "norm_refs": ["ЖК РФ", "ПП РФ №354"],
                "contexts": ["не являются нормативными", "но учитываются судами", "толкование спорных положений", "примеры расчётов", "формы документов"]
            },
            "письмо ростехнадзора": {
                "synonyms": ["разъяснения ростехнадзора", "письма надзорного ведомства", "контроль за соблюдением", "технические нормы"],
                "norm_refs": ["ПП РФ №491", "Правила технической эксплуатации"],
                "contexts": ["безопасность", "поверка", "техническое состояние", "ответственность УК", "использование в суде как доказательство"]
            },
            "позиция верховного суда": {
                "synonyms": ["мнение ВС РФ", "подход ВС РФ", "правовая позиция", "аргументация суда", "мотивировка решения"],
                "norm_refs": [],
                "contexts": ["цитируется в апелляциях", "основание для отмены решений", "формирование единообразной практики", "влияние на законодательство"]
            },
            "как суды трактуют": {
                "synonyms": ["толкование норм", "применение закона", "судебное толкование", "практика применения", "единый подход"],
                "norm_refs": [],
                "contexts": ["неясные формулировки", "пробелы в законе", "аналогия закона", "прецеденты", "разъяснения Пленума"]
            },
            "обзор практики по жкх": {
                "synonyms": ["обзор жилищных споров", "практика по капремонту", "справка по тарифам", "анализ по качеству услуг", "обобщение по долгам"],
                "norm_refs": [],
                "contexts": ["ВС РФ", "кассационные округа", "региональные суды", "по итогам года", "с примерами дел"]
            },
            "разъяснения контролирующих органов": {
                "synonyms": ["письма ГЖИ", "разъяснения Роспотребнадзора", "ответы ФАС", "методические рекомендации", "официальные комментарии"],
                "norm_refs": ["ЖК РФ", "ФЗ №59-ФЗ"],
                "contexts": ["не нормативные, но авторитетные", "используются в претензиях", "прилагаются к искам", "учитываются судами", "основание для проверок"]
            },
            "перспективы дела": {
                "synonyms": ["шансы на успех", "риски проигрыша", "вероятность удовлетворения", "что учитывает суд", "аргументы для победы"],
                "norm_refs": [],
                "contexts": ["наличие доказательств", "соблюдение досудебного порядка", "судебная практика", "позиция ВС РФ", "качество искового заявления"]
            },
            "оспаривание начислений": {
                "synonyms": ["неправильный расчёт", "завышенный тариф", "ошибка в квитанции", "возврат излишне уплаченного", "перерасчёт"],
                "norm_refs": ["ЖК РФ, ст. 157", "ПП РФ №354, п. 95"],
                "contexts": ["судебная практика ВС РФ", "обязанность УК предоставить расчёт", "доказательства ошибки", "сроки исковой давности"]
            },
            "ответственность ук": {
                "synonyms": ["взыскание убытков", "моральный вред", "неустойка", "штрафы", "регресс"],
                "norm_refs": ["ЖК РФ, ст. 161", "ГК РФ, ст. 1064"],
                "contexts": ["доказательства вины", "акты, фото, экспертиза", "судебная практика по компенсациям", "позиция ВС РФ по моральному вреду"]
            },
            "качество услуг": {
                "synonyms": ["некачественное отопление", "слабый напор воды", "отсутствие уборки", "антисанитария", "нарушение температурного режима"],
                "norm_refs": ["ПП РФ №354, раздел 6", "СанПиН 1.2.3685-21"],
                "contexts": ["замеры", "акты", "жалобы", "перерасчёт", "судебная практика по снижению платы"]
            },
            "взыскание долгов": {
                "synonyms": ["исковое заявление о взыскании", "судебный приказ", "неустойка", "пени", "ограничение выезда"],
                "norm_refs": ["ЖК РФ, ст. 155.1", "ФЗ №229-ФЗ"],
                "contexts": ["доказательства долга", "расчёт пени", "сроки давности", "позиция ВС РФ по завышению пени", "обжалование судебного приказа"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "vsrf.ru", "sudrf.ru", "kad.arbitr.ru", "rosreestr.gov.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".vsrf.ru", ".sudrf.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ВС РФ судебная практика ЖКХ")
        queries.append(f"{query} постановление Пленума ВС РФ жилищные споры")
        queries.append(f"{query} обзор практики Верховного Суда по ЖКХ")
        queries.append(f"{query} разъяснения Минстроя по ПП РФ 354")
        queries.append(f"{query} письма Ростехнадзора по поверке счётчиков")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Судебная практика и разъяснения ЖКХ
        Формирует системный промт:
        - Фокус: применение судебной практики и разъяснений
        - Жёсткая структура и ссылки на нормативные акты
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        # Проверка на необходимость расчета пени
        penalty_keywords = ["пени", "неустойка", "штраф за просрочку", "ставка цб", "9.5%", "ключевая ставка"]
        q_lower = summary.lower()
        should_calculate_penalty = any(kw in q_lower for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — эксперт по судебной практике и разъяснениям в сфере ЖКХ. "
            "Отвечай строго по нормативам и судебной практике, без выдуманных данных, используя только предоставленный контекст и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если информации нет — отвечай: 'Недостаточно данных для точного ответа.'\n"
            "2. Обязательно указывай ссылки на нормативные и судебные акты.\n"
            "3. Структура: краткий вывод → нормативы → судебная практика → практические рекомендации.\n"
            "4. Формулы пени включай только при упоминании пени.\n"
            "5. Приоритет источников: Постановления Пленума ВС РФ > Определения ВС РФ > Обзоры практики > Разъяснения Минстроя > Судебная практика нижестоящих судов.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "--- Основной ответ ---\n"
            "Краткий вывод: [позиция суда, шансы на успех, ключевые ссылки]\n"
            "Нормативное обоснование: [ЖК РФ, ПП РФ, ФЗ — точные статьи и пункты]\n"
            "Судебная практика:\n"
            "- Определение ВС РФ №XXX-ЭСXX-XXXX — краткая суть и значение\n"
            "- Постановление Пленума ВС РФ №X — ключевое разъяснение\n"
            "- Обзор судебной практики — выводы, типичные ошибки\n"
            "- Письмо Минстроя/Ростехнадзора №XXX — рекомендации и трактовка нормы\n\n"
            "Практические рекомендации:\n"
            "- Документы: акты, расчёты, переписка\n"
            "- Формулировки в иске: с ссылкой на позицию ВС РФ\n"
            "- Риски и контраргументы: позиция ответчика, практика аналогичных дел\n\n"
            "Ключевые источники:\n"
            "- Официальный сайт ВС РФ (https://www.vsrf.ru)\n"
            "- Судебные акты: kad.arbitr.ru, sudrf.ru, ГАС «Правосудие»\n"
            "- Разъяснения: Минстрой РФ, Ростехнадзор, ФАС, ГЖИ\n"
            "- Нормативы: ЖК РФ, ПП РФ №354, №491\n"
        )
    
        # --- Динамический блок: расчет пени ---
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчёт пени (если упомянут):**\n"
                "- Формула: Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Основание: [ЖК РФ, ст.155.1], [ФЗ №44-ФЗ], [ПП РФ №329]\n"
                "- Ограничение: не более 9.5% годовых до 2027 года\n"
                "- Пример: 10 000 руб., просрочка 30 дней → 95 руб.\n"
                "- Начало начисления: с 31-го дня после окончания срока оплаты\n"
            )
    
        system_prompt += f"{self.get_role_instruction(role)}"
    
        # --- Формат для QVikhr / LLaMA-3 ---
        prompt_formatted = (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )
    
        return prompt_formatted
        
class LicensingControlAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Лицензирование и контроль за УК", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "лицензия ук": {
                "synonyms": ["лицензия на управление", "лицензия МКД", "государственная лицензия", "получение лицензии", "продление лицензии"],
                "norm_refs": ["ФЗ №99-ФЗ, ст. 16", "ЖК РФ, ст. 193"],
                "contexts": ["обязательность", "срок действия (5 лет)", "условия получения", "реестр лицензий", "отказ в выдаче"]
            },
            "гжи": {
                "synonyms": ["госжилинспекция", "жилищная инспекция", "государственная жилищная инспекция", "контролирующий орган", "орган лицензирования"],
                "norm_refs": ["ЖК РФ, ст. 20", "ФЗ №294-ФЗ"],
                "contexts": ["проведение проверок", "выдача предписаний", "жалобы на УК", "отзыв лицензии", "реестр лицензий"]
            },
            "проверка ук": {
                "synonyms": ["проверка госжилинспекции", "внеплановая проверка", "плановая проверка", "выездная проверка", "документарная проверка"],
                "norm_refs": ["ФЗ №294-ФЗ, ст. 9-11", "ПП РФ №493"],
                "contexts": ["основания", "сроки", "уведомление", "длительность", "результаты проверки", "акт проверки"]
            },
            "отзыв лицензии": {
                "synonyms": ["аннулирование лицензии", "приостановление лицензии", "лишение лицензии", "отзыв лицензии ук", "приостановление деятельности"],
                "norm_refs": ["ФЗ №99-ФЗ, ст. 20", "ЖК РФ, ст. 193.1"],
                "contexts": ["нарушения", "неустранение нарушений", "жалобы жильцов", "судебное оспаривание", "последствия для УК"]
            },
            "жалоба в гжи": {
                "synonyms": ["обращение в жилинспекцию", "заявление на ук", "проверка по жалобе", "досудебное урегулирование", "обжалование действий ук"],
                "norm_refs": ["ФЗ №59-ФЗ, ст. 12", "ФЗ №294-ФЗ, ст. 10"],
                "contexts": ["форма жалобы", "срок рассмотрения (30 дней)", "результаты", "предписание", "внеплановая проверка"]
            },
            "предписание гжи": {
                "synonyms": ["предписание ук", "обязательное предписание", "исполнение предписания", "срок исполнения", "обжалование предписания"],
                "norm_refs": ["ФЗ №294-ФЗ, ст. 16", "ЖК РФ, ст. 20"],
                "contexts": ["содержание", "срок исполнения", "штраф за неисполнение", "обжалование в суде", "приоставление исполнения"]
            },
            "ответственность ук": {
                "synonyms": ["штраф для ук", "административная ответственность", "неустойка", "взыскание", "регресс"],
                "norm_refs": ["КоАП РФ, ст. 7.23", "ЖК РФ, ст. 161"],
                "contexts": ["размеры штрафов", "повторные нарушения", "дисквалификация", "судебные иски", "моральный вред"]
            },
            "реестр лицензий": {
                "synonyms": ["проверить лицензию ук", "единый реестр лицензий", "официальный реестр", "статус лицензии", "действующая лицензия"],
                "norm_refs": ["ФЗ №99-ФЗ, ст. 18", "ПП РФ №1110"],
                "contexts": ["где проверить (официальный сайт ГЖИ региона)", "информация в реестре", "дата выдачи/окончания", "основания для исключения"]
            },
            "условия лицензии": {
                "synonyms": ["требования к ук", "лицензионные требования", "стандарты управления", "квалификация сотрудников", "отчетность ук в гжи"],
                "norm_refs": ["ЖК РФ, ст. 193", "ФЗ №99-ФЗ, ст. 16"],
                "contexts": ["наличие квалифицированных сотрудников", "отсутствие судимости", "финансовая устойчивость", "предоставление отчетности", "соблюдение нормативов"]
            },
            "отчетность ук в гжи": {
                "synonyms": ["предоставление отчетов", "ежегодная отчетность", "отчет об исполнении договора", "информирование гжи", "раскрытие информации"],
                "norm_refs": ["ЖК РФ, ст. 161.1", "ПП РФ №731"],
                "contexts": ["сроки предоставления", "форма отчетов", "ответственность за не предоставление", "публикация в ГИС ЖКХ", "проверка достоверности"]
            },
            "основания для проверки": {
                "synonyms": ["повод для проверки", "жалоба жильцов", "истечение срока лицензии", "информация из СМИ", "результаты мониторинга"],
                "norm_refs": ["ФЗ №294-ФЗ, ст. 10", "ПП РФ №493"],
                "contexts": ["плановые проверки (раз в 3 года)", "внеплановые (по жалобам, авариям, СМИ)", "документарные/выездные", "сроки проведения"]
            },
            "обжалование действий гжи": {
                "synonyms": ["жалоба на гжи", "обжалование предписания", "оспаривание проверки", "вступление прокурора", "исковое заявление"],
                "norm_refs": ["ФЗ №294-ФЗ, ст. 22", "КоАП РФ, ст. 30.5"],
                "contexts": ["срок 10 дней", "подача в вышестоящий орган или суд", "приоставление исполнения", "восстановление сроков", "моральный вред"]
            },
            "приостановление лицензии": {
                "synonyms": ["временный запрет", "приостановка деятельности", "ограничение полномочий", "временный отзыв", "меры воздействия"],
                "norm_refs": ["ФЗ №99-ФЗ, ст. 20(1)", "ЖК РФ, ст. 193.1"],
                "contexts": ["угроза жизни/здоровью", "неоднократные нарушения", "неисполнение предписаний", "срок приостановления", "возобновление деятельности"]
            },
            "дисквалификация руководителя": {
                "synonyms": ["запрет занимать должность", "лишение права управления", "административное наказание", "персональная ответственность"],
                "norm_refs": ["КоАП РФ, ст. 3.11", "ФЗ №99-ФЗ, ст. 20(2)"],
                "contexts": ["грубые нарушения", "повторные нарушения", "срок до 3 лет", "влияние на лицензию УК", "судебное оспаривание"]
            },
            "судебная практика по лицензиям": {
                "synonyms": ["оспаривание отзыва лицензии", "обжалование предписаний гжи", "позиция вс рф по лицензированию", "судебные споры с ук"],
                "norm_refs": ["ГПК РФ, ст. 254", "КАС РФ, ст. 218"],
                "contexts": ["основания для отмены", "доказательства", "сроки", "роль прокурора", "последствия признания незаконным"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "reformagkh.ru", "gjirf.ru", "vsrf.ru", "kad.arbitr.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".gjirf.ru", ".vsrf.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ФЗ 99-ФЗ лицензия УК")
        queries.append(f"{query} ЖК РФ ст 193 лицензирование")
        queries.append(f"{query} судебная практика по отзыву лицензии УК")
        queries.append(f"{query} как проверить лицензию УК на сайте ГЖИ")
        queries.append(f"{query} образец жалобы в ГЖИ на УК")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Лицензирование и контроль за УК
        Формирует системный промт:
        - Фокус: лицензирование, проверка, контроль и ответственность УК
        - Жёсткая структура и ссылки на нормативные акты
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        # Проверка на необходимость расчета пени
        penalty_keywords = ["пени", "неустойка", "штраф за просрочку", "ставка цб", "9.5%", "ключевая ставка"]
        q_lower = summary.lower()
        should_calculate_penalty = any(kw in q_lower for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — эксперт по лицензированию и контролю управляющих компаний в сфере ЖКХ. "
            "Отвечай строго по нормативам, без выдуманных данных, используя только предоставленный контекст и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если информации нет — отвечай: 'Недостаточно данных для точного ответа.'\n"
            "2. Обязательно указывай ссылки на нормативные акты и судебные решения.\n"
            "3. Структура: краткий вывод → нормативы → пошаговая инструкция → судебная практика.\n"
            "4. Формулы пени включай только при упоминании пени.\n"
            "5. Приоритет источников: ЖК РФ > ФЗ №99-ФЗ > ФЗ №294-ФЗ > ПП РФ > судебная практика.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "--- Основной ответ ---\n"
            "Краткий вывод: [законно ли действие УК, куда обращаться, основные меры]\n"
            "Нормативное обоснование: [ЖК РФ, ФЗ №99-ФЗ, ФЗ №294-ФЗ, ПП РФ — точные статьи и пункты]\n"
            "Пошаговая инструкция:\n"
            "- Проверка лицензии УК (официальный реестр ГЖИ — ФЗ №99-ФЗ, ст.18)\n"
            "- Жалоба в ГЖИ (письменно, через ГИС ЖКХ — ФЗ №59-ФЗ, ст.12)\n"
            "- Последствия нарушений (предписание, штраф, приостановление, отзыв лицензии — ФЗ №99-ФЗ, ст.20)\n"
            "- Обжалование действий ГЖИ (вышестоящий орган или суд — ФЗ №294-ФЗ, ст.22)\n"
            "- Требования к УК (квалификация, отчётность, отсутствие судимости — ЖК РФ, ст.193)\n\n"
            "Судебная практика:\n"
            "- Определение ВС РФ №XXX-ЭСXX-XXXX — краткая позиция суда\n"
            "Если судебных решений нет: 'Судебная практика по данному вопросу в базе отсутствует'.\n\n"
            "Ключевые источники:\n"
            "- ЖК РФ, ФЗ №99-ФЗ, ФЗ №294-ФЗ, ПП РФ №493, ПП РФ №731\n"
        )
    
        # --- Динамический блок: расчет пени ---
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчёт пени (если упомянут):**\n"
                "- Формула: Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Основание: [ЖК РФ, ст.155.1], [ФЗ №44-ФЗ], [ПП РФ №329]\n"
                "- Ограничение: не более 9.5% годовых до 2027 года\n"
                "- Пример: 10 000 руб., просрочка 30 дней → 95 руб.\n"
                "- Начало начисления: с 31-го дня после окончания срока оплаты\n"
            )
    
        system_prompt += f"{self.get_role_instruction(role)}"
    
        # --- Формат для QVikhr / LLaMA-3 ---
        prompt_formatted = (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )
    
        return prompt_formatted
        
class RSOInteractionAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Взаимодействие с РСО", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "рсо": {
                "synonyms": ["ресурсоснабжающая организация", "поставщик ресурсов", "водоканал", "теплосеть", "энергосбыт", "газовая компания"],
                "norm_refs": ["ЖК РФ, ст. 157", "ПП РФ №354, раздел 10"],
                "contexts": ["обязанности", "договоры", "качество услуг", "начисления", "аварии", "ответственность"]
            },
            "прямой договор с рсо": {
                "synonyms": ["прямые платежи", "оплата напрямую", "договор с поставщиком", "разрыв с ук", "переход на прямые договоры"],
                "norm_refs": ["ЖК РФ, ст. 157.1", "ПП РФ №354, п. 105"],
                "contexts": ["условия перехода", "голосование ОСС", "расчёт платы", "ответственность РСО", "отказ УК"]
            },
            "акт сверки с рсо": {
                "synonyms": ["сверка начислений", "акт расхождений", "подтверждение задолженности", "согласование объёмов", "акт взаиморасчётов"],
                "norm_refs": ["ПП РФ №354, п. 101", "ГК РФ, ст. 409"],
                "contexts": ["периодичность", "состав комиссии", "обязательные реквизиты", "срок подписания", "использование в суде"]
            },
            "передача показаний рсо": {
                "synonyms": ["отправка показаний", "данные счётчиков", "интеграция с рсо", "автоматическая передача", "телеметрия"],
                "norm_refs": ["ПП РФ №354, п. 31(1)", "ФЗ №261-ФЗ, ст. 13"],
                "contexts": ["сроки (23-25 число)", "способы (лично, онлайн, через УК)", "ответственность за не передачу", "расчёт по среднему"]
            },
            "начисления рсо": {
                "synonyms": ["платеж рсо", "тариф рсо", "расчёт рсо", "объём потребления", "качество услуги рсо"],
                "norm_refs": ["ЖК РФ, ст. 157", "ПП РФ №354, раздел 10"],
                "contexts": ["основание для начисления", "расчёт по нормативу/счётчику", "ошибки в начислениях", "перерасчёт", "жалобы"]
            },
            "отключение рсо": {
                "synonyms": ["приостановка услуги", "ограничение подачи", "аварийное отключение", "плановое отключение", "отключение за неуплату"],
                "norm_refs": ["ПП РФ №354, п. 117-118", "ЖК РФ, ст. 157.1"],
                "contexts": ["уведомление (за 30 дней)", "неполное ограничение", "запрещённые услуги (отопление, холодная вода)", "восстановление"]
            },
            "качество услуги рсо": {
                "synonyms": ["некачественная услуга", "низкое давление", "норма температуры", "перебои", "замер параметров"],
                "norm_refs": ["ПП РФ №354, раздел 6", "СанПиН 1.2.3685-21"],
                "contexts": ["замер", "акт", "жалоба", "перерасчёт", "ответственность РСО", "штрафы"]
            },
            "граница балансовой принадлежности": {
                "synonyms": ["граница ответственности", "точка поставки", "разделение сетей", "внутридомовые сети", "магистральные сети"],
                "norm_refs": ["ПП РФ №491, п. 3", "ПП РФ №354, п. 103"],
                "contexts": ["определение в договоре", "схема сетей", "акт разграничения", "ответственность за аварии", "ремонт за чей счёт"]
            },
            "тепловая сеть": {
                "synonyms": ["теплоснабжение", "отопление", "горячее водоснабжение", "теплопровод", "ЦТП", "ИТП"],
                "norm_refs": ["ПП РФ №354, п. 54", "СанПиН 1.2.3685-21, п. 9.2"],
                "contexts": ["температура", "давление", "аварии", "ответственность РСО/УК", "расчёт по нормативу"]
            },
            "водопроводная сеть": {
                "synonyms": ["холодное водоснабжение", "водопровод", "напор воды", "качество воды", "авария на водопроводе"],
                "norm_refs": ["ПП РФ №354, п. 54(1)", "СанПиН 1.2.3685-21, п. 9.4"],
                "contexts": ["давление", "перебои", "замутнение", "акт замера", "жалоба в Роспотребнадзор"]
            },
            "канализационная сеть": {
                "synonyms": ["слив", "стоки", "засор канализации", "запах", "авария на канализации", "откачка"],
                "norm_refs": ["ПП РФ №354, п. 98(3)", "СанПиН 1.2.3685-21, п. 9.5"],
                "contexts": ["засоры", "затопления", "санитарные нормы", "срочный вызов", "ответственность за ремонт"]
            },
            "электросети": {
                "synonyms": ["электроснабжение", "отключение света", "напряжение", "перепады", "качество электроэнергии"],
                "norm_refs": ["ПП РФ №354, п. 54(3)", "Правила технической эксплуатации электроустановок"],
                "contexts": ["перебои", "короткое замыкание", "аварийное отключение", "жалоба в МЧС/прокуратуру", "расчёт по нормативу"]
            },
            "газовые сети": {
                "synonyms": ["газоснабжение", "утечка газа", "запах газа", "аварийное отключение", "проверка газового оборудования"],
                "norm_refs": ["ПП РФ №354, п. 54(4)", "Правила безопасности газораспределительных систем"],
                "contexts": ["угроза взрыва", "вызов аварийной службы", "проверка оборудования", "ответственность за утечку", "штрафы"]
            },
            "ответственность рсо": {
                "synonyms": ["обязанности рсо", "неисполнение обязательств", "взыскание убытков", "неустойка", "регресс"],
                "norm_refs": ["ЖК РФ, ст. 156", "ГК РФ, ст. 393"],
                "contexts": ["некачественная услуга", "аварии", "несвоевременное устранение", "договорные обязательства", "судебные иски"]
            },
            "интеграция с рсо": {
                "synonyms": ["обмен данными", "API с рсо", "автоматическая передача показаний", "единая информационная система", "ГИС ЖКХ"],
                "norm_refs": ["ФЗ №261-ФЗ, ст. 13(5)", "ПП РФ №354, п. 31(1)"],
                "contexts": ["технические требования", "форматы данных", "безопасность", "согласование с РСО", "ошибки передачи"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "roscomnadzor.ru", "mchs.gov.ru", "vsrf.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".mchs.gov.ru", ".vsrf.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ЖК РФ ст 157 РСО")
        queries.append(f"{query} ПП РФ 354 раздел 10")
        queries.append(f"{query} судебная практика по прямым договорам с РСО")
        queries.append(f"{query} акт сверки с ресурсоснабжающей организацией образец")
        queries.append(f"{query} граница балансовой принадлежности РСО УК")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Взаимодействие с РСО
        Формирует системный промт:
        - Фокус: договоры, передача показаний, акты сверки, границы балансовой принадлежности
        - Жёсткая структура и ссылки на нормативные акты
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        # Проверка на запрос о пени
        penalty_keywords = ["пени", "неустойка", "штраф за просрочку", "ставка цб", "9.5%", "ключевая ставка"]
        should_calculate_penalty = any(kw in summary.lower() for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — эксперт по взаимодействию управляющих компаний и ТСЖ с ресурсоснабжающими организациями (РСО) в сфере ЖКХ. "
            "Отвечай строго по нормативам, без выдуманных данных, используя только контекст, веб-результаты и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если информации нет — отвечай: 'Недостаточно данных для точного ответа.'\n"
            "2. Обязательно указывай ссылки на нормативные акты.\n"
            "3. Структура: краткий вывод → нормативы → пошаговая инструкция → судебная практика.\n"
            "4. Формулы пени включай только при упоминании пени.\n"
            "5. Приоритет источников: ЖК РФ > ПП РФ > разъяснения Минстроя > судебная практика.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "--- Основной ответ ---\n"
            "Краткий вывод: [кто отвечает, что делать, куда обращаться]\n"
            "Нормативное обоснование: [ЖК РФ, ПП РФ — точные статьи и пункты]\n"
            "Пошаговая инструкция:\n"
            "- Заключение прямого договора с РСО (ОСС — ЖК РФ, ст.157.1)\n"
            "- Передача показаний счетчиков (сроки, способы — ПП РФ №354, п.31)\n"
            "- Составление акта сверки (сроки, участники, реквизиты — ПП РФ №354, п.101)\n"
            "- Границы балансовой принадлежности (договор, схема — ПП РФ №491, п.3)\n"
            "- Оспаривание начислений РСО (жалоба, акт, суд — ЖК РФ, ст.157)\n\n"
            "Судебная практика:\n"
            "- Определение ВС РФ №XXX-ЭСXX-XXXX — краткая позиция суда\n"
            "Если судебных решений нет: 'Судебная практика по данному вопросу в базе отсутствует'.\n\n"
            "Ключевые нормативные акты:\n"
            "- ЖК РФ (ст.154 — состав платы, ст.156 — обязанности РСО, ст.157 — расчёты с РСО)\n"
            "- ПП РФ №354 (раздел 10 — взаимодействие с РСО, п.31 — передача показаний, п.101 — акты сверки)\n"
            "- ПП РФ №491 (п.3 — границы балансовой принадлежности)\n"
            "- СанПиН 1.2.3685-21 (параметры качества услуг)\n"
            "- ФЗ №261-ФЗ (обязанность установки ИПУ)\n"
        )
    
        # --- Динамический блок: расчет пени ---
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчёт пени (если упомянут):**\n"
                "- Формула: Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Основание: [ЖК РФ, ст.155.1], [ФЗ №44-ФЗ], [ПП РФ №329]\n"
                "- Ограничение: не более 9.5% годовых до 2027 года\n"
                "- Пример: 10 000 руб., просрочка 30 дней → 95 руб.\n"
                "- Начало начисления: с 31-го дня после окончания срока оплаты\n"
            )
    
        system_prompt += f"{self.get_role_instruction(role)}"
    
        # --- Формат для QVikhr / LLaMA-3 ---
        prompt_formatted = (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )
    
        return prompt_formatted

class SafetySecurityAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Безопасность и антитеррористическая защищенность", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "пожарная безопасность": {
                "synonyms": ["пожбез", "пожарная защита", "предотвращение пожара", "противопожарная защита", "пожарная профилактика"],
                "norm_refs": ["ПП РФ №1479", "ФЗ №69-ФЗ", "ЖК РФ, ст. 36"],
                "contexts": ["обязанности УК", "обязанности собственников", "проверки МЧС", "штрафы", "ликвидация нарушений"]
            },
            "антитеррор": {
                "synonyms": ["антитеррористическая защищенность", "антитеррористическая безопасность", "противодействие терроризму", "антитеррористические мероприятия"],
                "norm_refs": ["Постановление Правительства РФ №730", "ФЗ №35-ФЗ"],
                "contexts": ["паспорт безопасности", "инструктаж", "план эвакуации", "взаимодействие с полицией", "ответственность УК"]
            },
            "пожаротушение": {
                "synonyms": ["система пожаротушения", "автоматическое пожаротушение", "спринклерная система", "пожарный кран", "пожарный гидрант"],
                "norm_refs": ["ПП РФ №1479, п. 58", "СП 5.13130.2009"],
                "contexts": ["обязательность установки", "техническое обслуживание", "проверка работоспособности", "ответственность за неисправность"]
            },
            "пожарная сигнализация": {
                "synonyms": ["система оповещения", "оповещение о пожаре", "сирена", "звуковая сигнализация", "автоматическая пожарная сигнализация"],
                "norm_refs": ["ПП РФ №1479, п. 58", "СП 3.13130.2009"],
                "contexts": ["обязательность", "периодичность проверки", "интеграция с диспетчерской", "неисправность", "штрафы"]
            },
            "пожарный щит": {
                "synonyms": ["пожарный инвентарь", "пожарный уголок", "пожарный стенд", "огнетушитель", "пожарный рукав"],
                "norm_refs": ["ПП РФ №1479, п. 71", "СП 9.13130.2009"],
                "contexts": ["комплектация", "места размещения", "сроки замены", "проверка МЧС", "ответственность за отсутствие"]
            },
            "эвакуационный выход": {
                "synonyms": ["пожарный выход", "аварийный выход", "эвакуационный путь", "пожарная лестница", "запасной выход"],
                "norm_refs": ["ПП РФ №1479, п. 65", "СП 1.13130.2009"],
                "contexts": ["обязательная маркировка", "отсутствие загромождения", "освещение", "исправность дверей", "штрафы за нарушение"]
            },
            "пожарный кран": {
                "synonyms": ["пожарный шкаф", "внутренний пожарный кран", "ПК", "пожарный рукав", "пожарный ствол"],
                "norm_refs": ["ПП РФ №1479, п. 59", "СП 10.13130.2009"],
                "contexts": ["места установки", "исправность", "доступность", "проверка", "ответственность УК"]
            },
            "пожарный гидрант": {
                "synonyms": ["наружный пожарный гидрант", "гидрант", "пожарный водоисточник", "пожарный колодец"],
                "norm_refs": ["ПП РФ №1479, п. 60", "СП 8.13130.2009"],
                "contexts": ["доступность", "исправность", "сезонная подготовка", "ответственность за содержание", "проверка МЧС"]
            },
            "пожарный надзор": {
                "synonyms": ["мчс", "пожарная инспекция", "гпн", "проверка мчс", "инспектор мчс"],
                "norm_refs": ["ФЗ №294-ФЗ", "ФЗ №69-ФЗ"],
                "contexts": ["плановые и внеплановые проверки", "предписания", "штрафы", "обжалование", "приостановление деятельности"]
            },
            "пожарный сертификат": {
                "synonyms": ["сертификат пожарной безопасности", "декларация пожарной безопасности", "пожарный аудит", "проверка пожарной безопасности"],
                "norm_refs": ["ФЗ №123-ФЗ", "ПП РФ №1479"],
                "contexts": ["обязательность для новых зданий", "срок действия", "обновление при реконструкции", "ответственность за отсутствие"]
            },
            "пожарный минимум": {
                "synonyms": ["инструктаж по пожарной безопасности", "пожарный инструктаж", "обучение пожарной безопасности", "противопожарный инструктаж"],
                "norm_refs": ["ПП РФ №1479, п. 7", "Приказ МЧС №645"],
                "contexts": ["обязательность для сотрудников УК", "периодичность (1 раз в год)", "регистрация в журнале", "ответственность за необучение"]
            },
            "план эвакуации": {
                "synonyms": ["схема эвакуации", "эвакуационная схема", "маршрут эвакуации", "инструкция по эвакуации"],
                "norm_refs": ["ПП РФ №1479, п. 7", "ГОСТ Р 12.2.143-2009"],
                "contexts": ["обязательность размещения", "актуальность", "светящиеся таблички", "инструктаж жильцов", "проверка МЧС"]
            },
            "антитеррористический паспорт": {
                "synonyms": ["паспорт безопасности", "паспорт антитеррористической защищённости", "документ антитеррор", "паспорт объекта"],
                "norm_refs": ["Постановление Правительства РФ №730, п. 12", "Приказ ФСБ №555"],
                "contexts": ["обязательность для МКД", "срок действия (5 лет)", "обновление при реконструкции", "ответственность за отсутствие"]
            },
            "действия при пожаре": {
                "synonyms": ["алгоритм действий", "эвакуация", "вызов пожарных", "сообщить о пожаре", "пользование огнетушителем"],
                "norm_refs": ["ПП РФ №1479, п. 7", "Приказ МЧС №645"],
                "contexts": ["информирование жильцов", "тренировки", "инструкции в подъездах", "ответственность УК за отсутствие информации"]
            },
            "судебная практика по пожарной безопасности": {
                "synonyms": ["оспаривание штрафов мчс", "признание предписания незаконным", "взыскание ущерба", "ответственность ук за пожар"],
                "norm_refs": ["КоАП РФ, ст. 20.4", "ГК РФ, ст. 1064"],
                "contexts": ["основания для отмены", "доказательства устранения", "сроки обжалования", "моральный вред", "позиция ВС РФ"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "mchs.gov.ru", "fssb.ru", "vsrf.ru", "roscomnadzor.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".mchs.gov.ru", ".fssb.ru", ".vsrf.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ПП РФ 1479 пожарная безопасность")
        queries.append(f"{query} Постановление Правительства РФ 730 антитеррор")
        queries.append(f"{query} судебная практика по штрафам МЧС")
        queries.append(f"{query} требования к пожарному щиту в МКД")
        queries.append(f"{query} антитеррористический паспорт объекта ЖКХ")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Безопасность и антитеррористическая защищённость
        Формирует системный промт:
        - Фокус: пожарная безопасность, антитеррористическая защищённость
        - Жёсткая структура, ссылки на нормативы и судебную практику
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        # Проверка на запрос о пени
        penalty_keywords = ["пени", "неустойка", "штраф за просрочку", "ставка цб", "9.5%", "ключевая ставка"]
        should_calculate_penalty = any(kw in summary.lower() for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — экспертный ИИ-ассистент по пожарной безопасности и антитеррористической защищённости в сфере ЖКХ. "
            "Отвечай строго по нормативам, без выдуманных данных, используя только контекст, веб-результаты и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если информации нет — отвечай: 'Недостаточно данных для точного ответа.'\n"
            "2. Обязательно указывай ссылки на нормативные акты.\n"
            "3. Структура: краткий вывод → нормативы → пошаговая инструкция → судебная практика.\n"
            "4. Формулы пени включай только при упоминании пени.\n"
            "5. Приоритет: ФЗ > Постановления Правительства РФ > ПП РФ > Правила противопожарного режима > судебная практика.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "--- Основной ответ ---\n"
            "Краткий вывод: [что делать, кто отвечает, законно ли требование]\n"
            "Нормативное обоснование: [ФЗ, постановления, ПП РФ — точные номера и пункты]\n"
            "Пошаговая инструкция:\n"
            "- Обязанности УК (обеспечение исправности систем — ЖК РФ, ст.161.1; ПП РФ №1479)\n"
            "- Обязанности собственников (не загромождать эвакуационные пути — ЖК РФ, ст.36)\n"
            "- Подготовка к проверке МЧС (паспорт объекта, журналы инструктажей, исправность оборудования — ПП РФ №1479)\n"
            "- Действия при пожаре (вызов 101, эвакуация, использование огнетушителя — ПП РФ №1479, п.7)\n"
            "- Оформление антитеррористического паспорта (заказ в специализированной организации — Постановление №730)\n\n"
            "Судебная практика:\n"
            "- Определение ВС РФ №XXX-ЭСXX-XXXX — краткая позиция суда\n"
            "Если судебных решений нет: 'Судебная практика по данному вопросу в базе отсутствует'.\n\n"
            "Ключевые нормативные акты:\n"
            "- ЖК РФ (ст.36 — обязанности собственников, ст.161.1 — обязанности УК)\n"
            "- ФЗ №69-ФЗ «О пожарной безопасности»\n"
            "- ФЗ №35-ФЗ «О противодействии терроризму»\n"
            "- Постановление Правительства РФ №730 «О противодействии терроризму...»\n"
            "- ПП РФ №1479 «Правила противопожарного режима в РФ»\n"
            "- ФЗ №123-ФЗ «Технический регламент о требованиях пожарной безопасности»\n"
            "- Приказы МЧС и ФСБ по вопросам инструктажа и паспортизации\n"
        )
    
        # --- Динамический блок: расчет пени ---
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчёт пени (если упомянут):**\n"
                "- Формула: Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Основание: [ЖК РФ, ст.155.1], [ФЗ №44-ФЗ], [ПП РФ №329]\n"
                "- Ограничение: не более 9.5% годовых до 2027 года\n"
                "- Пример: 10 000 руб., просрочка 30 дней → 95 руб.\n"
                "- Начало начисления: с 31-го дня после окончания срока оплаты\n"
            )
    
        system_prompt += f"{self.get_role_instruction(role)}"
    
        # --- Формат для QVikhr / LLaMA-3 ---
        prompt_formatted = (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )
    
        return prompt_formatted

class EnergyEfficiencyAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Энергосбережение и энергоэффективность", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "энергосбережение": {
                "synonyms": ["энергоэффективность", "снижение потребления", "экономия ресурсов", "рациональное использование энергии"],
                "norm_refs": ["ФЗ №261-ФЗ, ст. 3", "ПП РФ №1818"],
                "contexts": ["обязанности УК", "меры по снижению", "государственные программы", "субсидии", "отчётность"]
            },
            "фз 261": {
                "synonyms": ["федеральный закон 261", "закон об энергосбережении", "261-фз", "энергетический закон"],
                "norm_refs": ["ФЗ №261-ФЗ"],
                "contexts": ["основные положения", "обязанности", "приборы учёта", "энергоаудит", "энергосервис", "ответственность"]
            },
            "энергетическое обследование": {
                "synonyms": ["энергоаудит", "аудит энергопотребления", "обследование здания", "тепловизионное обследование", "инструментальный замер"],
                "norm_refs": ["ФЗ №261-ФЗ, ст. 16", "Приказ Минстроя №889/пр"],
                "contexts": ["периодичность (1 раз в 5 лет)", "обязательность для МКД", "состав отчёта", "аккредитованные организации", "использование результатов"]
            },
            "одпу": {
                "synonyms": ["общедомовой прибор учета", "общедомовой счётчик", "домовой счётчик", "узел учёта", "вводной счётчик"],
                "norm_refs": ["ФЗ №261-ФЗ, ст. 13", "ПП РФ №354, п. 31"],
                "contexts": ["обязательность установки", "место установки", "поверка", "передача показаний", "расчёт платы", "ответственность за неисправность"]
            },
            "ипу": {
                "synonyms": ["индивидуальный прибор учета", "квартирный счётчик", "счётчик в квартире", "умный счётчик", "телеметрический счётчик"],
                "norm_refs": ["ФЗ №261-ФЗ, ст. 13", "ПП РФ №354, п. 31"],
                "contexts": ["обязательность установки", "сроки монтажа", "поверка", "автоматическая передача", "расчёт по показаниям", "штрафы за отсутствие"]
            },
            "тепловизионное обследование": {
                "synonyms": ["тепловизор", "теплосъёмка", "обследование теплопотерь", "диагностика фасада", "выявление мостиков холода"],
                "norm_refs": ["Приказ Минстроя №889/пр", "СП 50.13330.2012"],
                "contexts": ["часть энергоаудита", "сезонность (зимой)", "отчёт с фото", "рекомендации по утеплению", "использование в капремонте"]
            },
            "утепление фасада": {
                "synonyms": ["теплоизоляция", "модернизация фасада", "энергосберегающий фасад", "вентилируемый фасад", "шуба"],
                "norm_refs": ["ФЗ №261-ФЗ, ст. 25", "СП 23-101-2004"],
                "contexts": ["включение в капремонт", "расчёт экономии", "материалы", "господдержка", "снижение теплопотерь", "срок окупаемости"]
            },
            "замена окон": {
                "synonyms": ["пластиковые окна", "энергосберегающие окна", "стеклопакеты", "замена деревянных окон", "окна с энергосбережением"],
                "norm_refs": ["ФЗ №261-ФЗ, ст. 25", "ГОСТ 30674-99"],
                "contexts": ["в МКД и подъездах", "требования к коэффициенту сопротивления", "госпрограммы", "энергосервис", "снижение теплопотерь"]
            },
            "модернизация систем": {
                "synonyms": ["замена котлов", "модернизация лифтов", "автоматизация ИТП", "регулирование отопления", "внедрение АСУ"],
                "norm_refs": ["ФЗ №261-ФЗ, ст. 25", "ПП РФ №1289"],
                "contexts": ["энергосервисные контракты", "расчёт экономии", "господдержка", "технические требования", "срок окупаемости"]
            },
            "энергосервисный контракт": {
                "synonyms": ["эск", "энергосервис", "контракт энергосервиса", "энергосервисная компания", "оплата за счёт экономии"],
                "norm_refs": ["ФЗ №261-ФЗ, ст. 14", "ПП РФ №1289"],
                "contexts": ["модель финансирования", "расчёт экономии", "срок контракта", "гарантии подрядчика", "ответственность", "судебная практика"]
            },
            "энергосберегающие технологии": {
                "synonyms": ["светодиодное освещение", "частотные преобразователи", "терморегуляторы", "умные системы", "автоматизация"],
                "norm_refs": ["ФЗ №261-ФЗ, ст. 25", "СП 60.13330.2016"],
                "contexts": ["внедрение в МКД", "расчёт эффективности", "господдержка", "обучение персонала", "мониторинг экономии"]
            },
            "обязанности собственников": {
                "synonyms": ["обязанности жильцов", "обязанности по энергосбережению", "установка счётчиков", "допуск к обследованию", "сохранение документов"],
                "norm_refs": ["ФЗ №261-ФЗ, ст. 9", "ЖК РФ, ст. 36"],
                "contexts": ["установка ИПУ", "допуск в квартиру", "сохранение актов", "участие в ОСС по энергоэффективности", "ответственность за отказ"]
            },
            "государственная поддержка": {
                "synonyms": ["субсидии", "гранты", "компенсации", "льготные кредиты", "возмещение затрат"],
                "norm_refs": ["ФЗ №261-ФЗ, ст. 25.1", "ПП РФ №1289"],
                "contexts": ["условия получения", "порядок оформления", "документы", "региональные программы", "энергосервис", "капремонт"]
            },
            "ответственность за нарушения": {
                "synonyms": ["штрафы", "административная ответственность", "предписание", "приостановление деятельности", "судебные иски"],
                "norm_refs": ["ФЗ №261-ФЗ, ст. 27", "КоАП РФ, ст. 9.16"],
                "contexts": ["отсутствие счётчиков", "не проведён энергоаудит", "фальсификация отчётов", "жалобы", "обжалование штрафов"]
            },
            "судебная практика по фз 261": {
                "synonyms": ["оспаривание штрафов", "споры по энергосервису", "обязанность установки счётчиков", "ответственность ук", "позиция вс рф"],
                "norm_refs": ["ГК РФ, ст. 421", "КАС РФ, ст. 218"],
                "contexts": ["основания для отмены", "доказательства экономии", "сроки исковой давности", "роль энергоаудита", "исполнение решений"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "roscomnadzor.ru", "mce.gov.ru", "vsrf.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".mce.gov.ru", ".vsrf.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ФЗ 261-ФЗ энергосбережение")
        queries.append(f"{query} ПП РФ 1289 энергосервис")
        queries.append(f"{query} судебная практика по энергосервисным контрактам")
        queries.append(f"{query} требования к установке ИПУ ОДПУ")
        queries.append(f"{query} тепловизионное обследование МКД нормы")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Энергосбережение и энергоэффективность
        Формирует системный промт:
        - Фокус: энергосбережение, энергоэффективность, ИПУ, энергосервис, господдержка
        - Жёсткая структура, ссылки на нормативы и судебную практику
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        # Проверка на запрос о пени
        penalty_keywords = ["пени", "неустойка", "штраф за просрочку", "ставка цб", "9.5%", "ключевая ставка"]
        should_calculate_penalty = any(kw in summary.lower() for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — экспертный ИИ-ассистент по энергосбережению и энергоэффективности в сфере ЖКХ. "
            "Отвечай строго по нормативам, без выдуманных данных, используя только контекст, веб-результаты и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если информации нет — отвечай: 'Недостаточно данных для точного ответа.'\n"
            "2. Обязательно указывай ссылки на нормативные акты.\n"
            "3. Структура: краткий вывод → нормативы → пошаговая инструкция → судебная практика.\n"
            "4. Формулы пени включай только при упоминании пени.\n"
            "5. Приоритет: ФЗ №261-ФЗ > ПП РФ > Приказы Минстроя > судебная практика.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "--- Основной ответ ---\n"
            "Краткий вывод: [что делать, кто отвечает, законно ли требование]\n"
            "Нормативное обоснование: [ФЗ, постановления, приказы — точные номера и пункты]\n"
            "Пошаговая инструкция:\n"
            "- Проведение энергетического обследования (сроки, аккредитованная организация — ФЗ №261-ФЗ, ст.16; Приказ Минстроя №889/пр)\n"
            "- Установка и поверка ИПУ/ОДПУ (сроки, передача показаний — ФЗ №261-ФЗ, ст.13)\n"
            "- Заключение энергосервисного контракта (расчёт экономии, срок контракта — ПП РФ №1289)\n"
            "- Реализация мер энергоэффективности (утепление, модернизация систем — ФЗ №261-ФЗ, ст.25)\n"
            "- Получение господдержки (документы, программы — ПП РФ №1289)\n\n"
            "Судебная практика:\n"
            "- Определение ВС РФ №XXX-ЭСXX-XXXX — краткая позиция суда\n"
            "Если судебных решений нет: 'Судебная практика по данному вопросу в базе отсутствует'.\n\n"
            "Ключевые нормативные акты:\n"
            "- ФЗ №261-ФЗ «Об энергосбережении и о повышении энергетической эффективности»\n"
            "- ПП РФ №1289 «О требованиях к энергосервисным контрактам...»\n"
            "- ПП РФ №1818 «Об утверждении Правил установления требований энергетической эффективности...»\n"
            "- Приказ Минстроя №889/пр «Об утверждении Правил проведения энергетического обследования...»\n"
            "- ПП РФ №354 (в части установки и поверки приборов учёта)\n"
        )
    
        # --- Динамический блок: расчет пени ---
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчёт пени (если упомянут):**\n"
                "- Формула: Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Основание: [ЖК РФ, ст.155.1], [ФЗ №44-ФЗ], [ПП РФ №329]\n"
                "- Ограничение: не более 9.5% годовых до 2027 года\n"
                "- Пример: 10 000 руб., просрочка 30 дней → 95 руб.\n"
                "- Начало начисления: с 31-го дня после окончания срока оплаты\n"
            )
    
        system_prompt += f"{self.get_role_instruction(role)}"
    
        # --- Формат для QVikhr / LLaMA-3 ---
        prompt_formatted = (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )
    
        return prompt_formatted
        
class ReceiptProcessingAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Обработка чеков и платежных документов", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "фискальный чек": {
                "synonyms": ["кассовый чек", "электронный чек", "бумажный чек", "чек онлайн-кассы", "фискальный документ"],
                "norm_refs": ["ФЗ №54-ФЗ, ст. 4.7", "Приказ ФНС №ЕД-7-20/662@"],
                "contexts": ["обязательность выдачи", "реквизиты", "хранение", "электронный формат", "QR-код"]
            },
            "qr-код": {
                "synonyms": ["qr", "qr-code", "фискальный qr", "скан qr", "чек по qr"],
                "norm_refs": ["ФЗ №54-ФЗ, ст. 4.7(5)", "Приказ ФНС №ММВ-7-20/229@"],
                "contexts": ["структура", "расшифровка", "ошибки сканирования", "интеграция с приложениями", "проверка подлинности"]
            },
            "офд": {
                "synonyms": ["оператор фискальных данных", "офд чеков", "передача данных в офд", "архив чеков", "личный кабинет офд"],
                "norm_refs": ["ФЗ №54-ФЗ, ст. 4.2", "Приказ ФНС №ЕД-7-20/662@"],
                "contexts": ["обязательность подключения", "сроки хранения (5 лет)", "передача данных", "ошибки передачи", "ответственность"]
            },
            "теги чека": {
                "synonyms": ["тег 1008", "тег 1020", "тег 1054", "тег 1055", "тег 1081", "тег 1102", "тег 1162", "тег 1163", "тег 1187", "тег 1192", "тег 1203", "тег 1207", "тег 1227"],
                "norm_refs": ["Приказ ФНС №ММВ-7-20/229@", "ФФД 1.2"],
                "contexts": ["расшифровка тегов", "обязательные теги", "признак расчёта", "предмет расчёта", "налоговая ставка", "платежный агент"]
            },
            "фискальный накопитель": {
                "synonyms": ["фн", "фискал", "фискальный регистратор", "замена фн", "срок службы фн"],
                "norm_refs": ["ФЗ №54-ФЗ, ст. 4.1", "Приказ ФНС №ЕД-7-20/662@"],
                "contexts": ["срок замены (13/15 месяцев)", "регистрация", "блокировка", "архив фн", "отчётность"]
            },
            "фискальный признак": {
                "synonyms": ["фпд", "фискальный признак документа", "контрольная сумма чека", "уникальный идентификатор"],
                "norm_refs": ["ФЗ №54-ФЗ, ст. 4.7(1)", "Приказ ФНС №ММВ-7-20/229@"],
                "contexts": ["расчёт", "проверка подлинности", "ошибки в фпд", "дублирование", "восстановление"]
            },
            "ошибка в чеке": {
                "synonyms": ["неверный чек", "чек не проходит", "чек не считывается", "некорректный тег", "битый qr-код"],
                "norm_refs": ["ФЗ №54-ФЗ, ст. 14.5", "КоАП РФ, ст. 14.5"],
                "contexts": ["причины", "исправление", "аннулирование", "повторная печать", "штрафы", "жалоба в ФНС"]
            },
            "автоматическая обработка": {
                "synonyms": ["интеграция с бухгалтерией", "парсинг чеков", "распознавание чеков", "ocr чеков", "api офд"],
                "norm_refs": [],
                "contexts": ["форматы (xml, json)", "библиотеки", "интеграционные платформы", "валидация данных", "ошибки парсинга"]
            },
            "xml чек": {
                "synonyms": ["json чек", "электронный формат чека", "структура чека", "файл чека", "выгрузка чеков"],
                "norm_refs": ["Приказ ФНС №ММВ-7-20/229@", "ФФД 1.2"],
                "contexts": ["схема xml", "обязательные элементы", "валидация", "подпись", "хранение", "передача в бухгалтерию"]
            },
            "бсо": {
                "synonyms": ["бланк строгой отчетности", "бумажный бсо", "электронный бсо", "бсо вместо кассового чека"],
                "norm_refs": ["ФЗ №54-ФЗ, ст. 2.1", "Приказ ФНС №ММВ-7-20/229@"],
                "contexts": ["когда можно использовать", "реквизиты", "регистрация", "замена на онлайн-кассу", "ответственность"]
            },
            "онлайн-касса": {
                "synonyms": ["ккт", "контрольно-кассовая техника", "касса 54-фз", "новая касса", "регистрация ккт"],
                "norm_refs": ["ФЗ №54-ФЗ", "Приказ ФНС №ЕД-7-20/662@"],
                "contexts": ["обязательность", "регистрация в ФНС", "подключение к ОФД", "обслуживание", "штрафы за нарушение"]
            },
            "платежный агент": {
                "synonyms": ["посредник", "агент по приёму платежей", "расчётный агент", "оператор платежей", "агент банка"],
                "norm_refs": ["ФЗ №54-ФЗ, ст. 1.2(10)", "Приказ ФНС №ММВ-7-20/229@"],
                "contexts": ["обязательные теги (1020, 1054)", "реквизиты агента", "комиссия", "отчётность", "ответственность"]
            },
            "поставщик": {
                "synonyms": ["продавец", "организация", "инн поставщика", "наименование поставщика", "реквизиты поставщика"],
                "norm_refs": ["ФЗ №54-ФЗ, ст. 4.7(1)", "Приказ ФНС №ММВ-7-20/229@"],
                "contexts": ["обязательные реквизиты в чеке", "инн/кпп", "адрес", "наименование", "ошибки в реквизитах"]
            },
            "признак расчёта": {
                "synonyms": ["тег 1054", "приход", "возврат прихода", "расход", "возврат расхода", "аванс", "кредит"],
                "norm_refs": ["Приказ ФНС №ММВ-7-20/229@", "ФФД 1.2"],
                "contexts": ["значения тега", "обязательность", "ошибки", "влияние на бухучёт", "налоговые последствия"]
            },
            "судебная практика по чекам": {
                "synonyms": ["оспаривание штрафов за чеки", "признание чека недействительным", "ответственность за ошибки в чеках", "позиция вс рф по 54-фз"],
                "norm_refs": ["КАС РФ, ст. 218", "КоАП РФ, ст. 14.5"],
                "contexts": ["основания для отмены штрафа", "доказательства исправления", "сроки обжалования", "технические сбои", "добросовестность"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "nalog.gov.ru", "ofd.ru", "fns.ru", "vsrf.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".nalog.gov.ru", ".fns.ru", ".vsrf.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ФЗ 54-ФЗ чеки")
        queries.append(f"{query} Приказ ФНС ММВ-7-20/229@ теги чека")
        queries.append(f"{query} судебная практика по ошибкам в фискальных чеках")
        queries.append(f"{query} как расшифровать QR-код чека")
        queries.append(f"{query} интеграция чеков с 1С бухгалтерией")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Обработка чеков и платежных документов
        Формирует системный промт:
        - Фокус: фискальные чеки, QR-коды, теги ФФД, исправление ошибок, интеграция, ФЗ №54-ФЗ
        - Жёсткая структура, ссылки на нормативы и судебную практику
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        # Проверка на запрос о пени
        penalty_keywords = ["пени", "неустойка", "штраф за просрочку", "ставка цб", "9.5%", "ключевая ставка"]
        should_calculate_penalty = any(kw in summary.lower() for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — экспертный ИИ-ассистент по обработке фискальных чеков и платежных документов в сфере ЖКХ. "
            "Отвечай строго по нормативам, без выдуманных данных, используя только контекст, веб-результаты и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если информации нет — отвечай: 'Недостаточно данных для точного ответа.'\n"
            "2. Обязательно указывай ссылки на нормативные акты.\n"
            "3. Структура: краткий вывод → нормативы → пошаговая инструкция → судебная практика.\n"
            "4. Формулы пени включай только при упоминании пени.\n"
            "5. Приоритет: ФЗ №54-ФЗ > Приказы ФНС > ПП РФ > судебная практика.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "--- Основной ответ ---\n"
            "Краткий вывод: [что делать, как исправить, куда обратиться]\n"
            "Нормативное обоснование: [ФЗ, приказы, ПП — точные номера и пункты]\n"
            "Пошаговая инструкция / Техническое решение:\n"
            "- Расшифровка QR-кода и тегов чека (структура, обязательные поля — Приказ ФНС №ММВ-7-20/229@)\n"
            "- Исправление ошибок в чеке (аннулирование, повторная печать — ФЗ №54-ФЗ, ст.4.7)\n"
            "- Интеграция чеков с бухгалтерией (форматы XML/JSON, API ОФД — Приказ ФНС №ЕД-7-20/662@)\n"
            "- Обязательные данные в чеке (ИНН, признак расчёта, ФПД — ФЗ №54-ФЗ, ст.4.7)\n"
            "- Действия при ошибках передачи в ОФД (проверка сети, перезагрузка ККТ, обращение в техподдержку)\n\n"
            "Судебная практика:\n"
            "- Определение ВС РФ №XXX-ЭСXX-XXXX — краткая позиция суда\n"
            "Если судебных решений нет: 'Судебная практика по данному вопросу в базе отсутствует'.\n\n"
            "Ключевые нормативные акты:\n"
            "- ФЗ №54-ФЗ «О применении контрольно-кассовой техники»\n"
            "- Приказ ФНС №ММВ-7-20/229@ «О формате фискальных документов»\n"
            "- Приказ ФНС №ЕД-7-20/662@ «Об утверждении порядка регистрации ККТ»\n"
            "- ПП РФ №354 (в части платежных документов ЖКХ)\n"
            "- ФФД 1.2 (формат фискальных данных)\n"
        )
    
        # --- Динамический блок: расчет пени ---
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчёт пени (если упомянут):**\n"
                "- Формула: Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Основание: [ЖК РФ, ст.155.1], [ФЗ №44-ФЗ], [ПП РФ №329]\n"
                "- Ограничение: не более 9.5% годовых до 2027 года\n"
                "- Пример: 10 000 руб., просрочка 30 дней → 95 руб.\n"
                "- Начало начисления: с 31-го дня после окончания срока оплаты\n"
            )
    
        system_prompt += f"{self.get_role_instruction(role)}"
    
        # --- Формат для QVikhr / LLaMA-3 ---
        prompt_formatted = (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )
    
        return prompt_formatted
        
class PassportRegistrationAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Паспортный учет и регистрация", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "прописка": {
                "synonyms": ["регистрация", "постоянная регистрация", "постановка на регистрационный учет", "оформить прописку", "прописаться"],
                "norm_refs": ["ФЗ №5242-1", "ПП РФ №713, п. 9"],
                "contexts": ["документы", "сроки", "где оформить", "через госуслуги", "ответственность за нарушение"]
            },
            "выписка": {
                "synonyms": ["снятие с регистрационного учета", "выписаться", "аннулирование регистрации", "снятие с учёта", "отказ от регистрации"],
                "norm_refs": ["ПП РФ №713, п. 28", "ФЗ №5242-1"],
                "contexts": ["добровольная", "принудительная", "по суду", "автоматическая при регистрации по новому месту", "документы"]
            },
            "документы для регистрации": {
                "synonyms": ["что нужно для прописки", "документы паспортисту", "форма №6", "заявление о регистрации", "документы собственника"],
                "norm_refs": ["ПП РФ №713, п. 15", "Приказ МВД №984"],
                "contexts": ["паспорт", "заявление по форме №6", "документ-основание (договор, свидетельство)", "согласие собственника", "доверенность"]
            },
            "где оформить регистрацию": {
                "synonyms": ["паспортный стол", "мфц регистрация", "госуслуги прописка", "омвд", "отдел по вопросам миграции", "миграционный пункт"],
                "norm_refs": ["ПП РФ №713, п. 10", "ФЗ №5242-1"],
                "contexts": ["личный визит", "электронная подача", "почта", "мфц", "госуслуги", "сроки оказания услуги"]
            },
            "временная регистрация": {
                "synonyms": ["регистрация по месту пребывания", "форма №3", "временная прописка", "регистрация на срок", "миграционный учет"],
                "norm_refs": ["ПП РФ №713, п. 20", "ФЗ №109-ФЗ"],
                "contexts": ["срок действия", "документы", "обязанность уведомления", "штрафы за отсутствие", "отличие от постоянной"]
            },
            "обязанности собственника": {
                "synonyms": ["что должен собственник", "согласие на прописку", "уведомление о регистрации", "ответственность за жильцов", "не прописывать"],
                "norm_refs": ["ЖК РФ, ст. 31", "ПП РФ №713, п. 32", "КоАП РФ, ст. 19.15.1"],
                "contexts": ["согласие на регистрацию", "уведомление ФМС", "ответственность за фиктивную регистрацию", "право на выселение", "ограничения"]
            },
            "обязанности ук": {
                "synonyms": ["действия управляющей компании", "уведомление о регистрации", "передача данных", "сотрудничество с паспортным столом", "ответственность ук"],
                "norm_refs": ["ЖК РФ, ст. 31(3)", "ПП РФ №713, п. 32"],
                "contexts": ["передача сведений в ОВМ", "сроки (3 дня)", "форма уведомления", "штрафы за неисполнение", "взаимодействие с жильцами"]
            },
            "форма №6": {
                "synonyms": ["заявление о регистрации", "бланк регистрации", "форма регистрации по месту жительства", "заполнение формы 6"],
                "norm_refs": ["Приказ МВД №984, Приложение 2", "ПП РФ №713"],
                "contexts": ["образец заполнения", "где получить", "электронная форма", "подпись заявителя и собственника", "нотариальное заверение"]
            },
            "форма №7": {
                "synonyms": ["листок убытия", "заявление о снятии с регистрационного учета", "форма снятия с учета", "заполнение формы 7"],
                "norm_refs": ["Приказ МВД №984, Приложение 4", "ПП РФ №713"],
                "contexts": ["когда нужна", "добровольное снятие", "по суду", "автоматическое снятие", "архивные справки"]
            },
            "справка о регистрации": {
                "synonyms": ["подтверждение регистрации", "адресная справка", "выписка из домовой книги", "форма №9", "форма №8"],
                "norm_refs": ["ПП РФ №713, п. 36", "Приказ МВД №984"],
                "contexts": ["где получить", "срок действия", "для каких целей", "электронная версия", "платность услуги"]
            },
            "сроки оформления": {
                "synonyms": ["сколько делается прописка", "срок регистрации", "срок выписки", "сроки оказания услуги", "срочная регистрация"],
                "norm_refs": ["ПП РФ №713, п. 21", "ФЗ №5242-1"],
                "contexts": ["3 дня при личной подаче", "8 дней при подаче через МФЦ/почту", "автоматическое снятие при новой регистрации", "штрафы за просрочку"]
            },
            "фиктивная регистрация": {
                "synonyms": ["фальшивая прописка", "регистрация без проживания", "покупка регистрации", "незаконная прописка", "ответственность за фиктивную регистрацию"],
                "norm_refs": ["УК РФ, ст. 322.2", "ПП РФ №713, п. 32"],
                "contexts": ["признаки", "наказание (штраф, лишение свободы)", "обязанность УК и собственников сообщать", "проверки МВД", "судебная практика"]
            },
            "регистрация несовершеннолетних": {
                "synonyms": ["прописка ребенка", "регистрация по месту жительства родителей", "согласие собственника на ребенка", "новорожденный", "дети до 14 лет"],
                "norm_refs": ["ПП РФ №713, п. 27", "ФЗ №5242-1"],
                "contexts": ["автоматическая регистрация с родителями", "документы", "согласие не требуется", "особенности регистрации подростков", "штамп в свидетельстве"]
            },
            "военнообязанные": {
                "synonyms": ["военный билет", "регистрация военнообязанных", "постановка на воинский учет", "военкомат", "уведомление военкомата"],
                "norm_refs": ["ФЗ «О воинской обязанности», ст. 10", "ПП РФ №713"],
                "contexts": ["обязанность уведомлять военкомат", "сроки", "документы", "штрафы за нарушение", "взаимодействие с паспортным столом"]
            },
            "судебная практика по регистрации": {
                "synonyms": ["оспаривание отказа в регистрации", "выселение за фиктивную прописку", "обязанность ук уведомлять", "позиция вс рф по регистрации"],
                "norm_refs": ["ГПК РФ, ст. 254", "КАС РФ, ст. 218"],
                "contexts": ["основания для признания регистрации недействительной", "доказательства проживания", "сроки исковой давности", "роль свидетелей", "исполнение решений"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "мвд.рф", "госуслуги.рф", "мфц.рф", "vsrf.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".мвд.рф", ".госуслуги.рф", ".мфц.рф", ".vsrf.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ПП РФ 713 регистрация")
        queries.append(f"{query} ФЗ 5242-1 прописка")
        queries.append(f"{query} судебная практика по фиктивной регистрации")
        queries.append(f"{query} документы для прописки через Госуслуги")
        queries.append(f"{query} обязанности УК при регистрации граждан")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Паспортный учет и регистрация
        Формирует системный промт:
        - Фокус: оформление и снятие граждан с регистрационного учета, обязанности собственников и УК
        - Жёсткая структура, ссылки на нормативы и судебную практику
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        # Проверка на запрос о пени
        penalty_keywords = ["пени", "неустойка", "штраф за просрочку", "ставка цб", "9.5%", "ключевая ставка"]
        should_calculate_penalty = any(kw in summary.lower() for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — экспертный ИИ-ассистент по паспортному учёту и регистрации граждан в сфере ЖКХ. "
            "Отвечай строго по нормативам, без выдуманных данных, используя только контекст, веб-результаты и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если информации нет — отвечай: 'Недостаточно данных для точного ответа.'\n"
            "2. Обязательно указывай ссылки на нормативные акты.\n"
            "3. Структура: краткий вывод → нормативы → пошаговая инструкция → судебная практика.\n"
            "4. Формулы пени включай только при упоминании пени.\n"
            "5. Приоритет: ФЗ > ПП РФ > ЖК РФ > Приказы МВД > судебная практика.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "--- Основной ответ ---\n"
            "Краткий вывод: [что делать, куда идти, какие документы нужны]\n"
            "Нормативное обоснование: [ФЗ, ПП РФ, ЖК РФ — точные номера и пункты]\n"
            "Пошаговая инструкция:\n"
            "- Оформление постоянной/временной регистрации (документы, сроки, способы подачи — ПП РФ №713, п.9, 20)\n"
            "- Выписка из квартиры (добровольно, автоматически, через суд — ПП РФ №713, п.28)\n"
            "- Обязанности собственника (согласие, уведомление — ЖК РФ, ст.31; ПП РФ №713, п.32)\n"
            "- Обязанности УК (уведомление ОВМ в 3-дневный срок — ЖК РФ, ст.31(3))\n"
            "- Получение справки о регистрации (МФЦ, Госуслуги, паспортный стол — ПП РФ №713, п.36)\n\n"
            "Судебная практика:\n"
            "- Определение ВС РФ №XXX-ЭСXX-XXXX — краткая позиция суда\n"
            "Если судебных решений нет: 'Судебная практика по данному вопросу в базе отсутствует'.\n\n"
            "Ключевые нормативные акты:\n"
            "- ФЗ №5242-1 «О праве граждан РФ на свободу передвижения…»\n"
            "- ПП РФ №713 «О регистрации и снятии граждан с регистрационного учета…»\n"
            "- ЖК РФ, ст.31 — права и обязанности собственников и нанимателей\n"
            "- ФЗ «О воинской обязанности и военной службе» (для военнообязанных)\n"
            "- УК РФ, ст.322.2 — фиктивная регистрация\n"
            "- Приказ МВД №984 «Об утверждении Административного регламента…»\n"
        )
    
        # --- Динамический блок: расчет пени ---
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчёт пени (если упомянут):**\n"
                "- Формула: Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Основание: [ЖК РФ, ст.155.1], [ФЗ №44-ФЗ], [ПП РФ №329]\n"
                "- Ограничение: не более 9.5% годовых до 2027 года\n"
                "- Пример: 10 000 руб., просрочка 30 дней → 95 руб.\n"
                "- Начало начисления: с 31-го дня после окончания срока оплаты\n"
            )
    
        system_prompt += f"{self.get_role_instruction(role)}"
    
        # --- Формат для QVikhr / LLaMA-3 ---
        prompt_formatted = (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )
    
        return prompt_formatted

class RecalculationAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Перерасчеты ЖКУ", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "перерасчет": {
                "synonyms": ["перерасчёт", "корректировка", "доначисление", "возврат средств", "пересчет"],
                "norm_refs": ["ПП РФ №354, раздел 6", "ПП РФ №354, п. 90-91"],
                "contexts": ["временное отсутствие", "некачественная услуга", "ошибка в начислении", "поверка счётчика", "отключение"]
            },
            "временное отсутствие": {
                "synonyms": ["отпуск", "командировка", "больница", "уехал", "отсутствие более 5 дней"],
                "norm_refs": ["ПП РФ №354, п. 86", "ПП РФ №354, п. 90"],
                "contexts": ["максимум 6 месяцев", "документы (билеты, справки)", "заявление в течение 30 дней после возвращения", "исключения (отопление, ОДН)"]
            },
            "некачественная услуга": {
                "synonyms": ["некачественное отопление", "слабый напор воды", "отсутствие горячей воды", "антисанитария", "нарушение температурного режима"],
                "norm_refs": ["ПП РФ №354, раздел 6", "ПП РФ №354, п. 90"],
                "contexts": ["акт о нарушении", "замер параметров", "жалоба", "расчёт по формуле", "срок устранения"]
            },
            "ипу": {
                "synonyms": ["индивидуальный прибор учета", "счётчик", "водомер", "электросчётчик", "теплосчётчик"],
                "norm_refs": ["ПП РФ №354, п. 31", "ПП РФ №354, п. 81"],
                "contexts": ["расчёт по показаниям", "поверка", "истёк срок поверки", "начисление по нормативу", "перерасчёт после поверки"]
            },
            "одпу": {
                "synonyms": ["общедомовой прибор учета", "домовой счётчик", "узел учёта", "ОДПУ"],
                "norm_refs": ["ПП РФ №354, п. 40", "ПП РФ №354, п. 42"],
                "contexts": ["расчёт по среднему", "выход из строя", "начисление по нормативу", "перерасчёт после ремонта", "ответственность УК"]
            },
            "отключение": {
                "synonyms": ["отключение услуги", "приостановка", "ограничение", "аварийное отключение", "плановое отключение"],
                "norm_refs": ["ПП РФ №354, п. 98", "ПП РФ №354, п. 117"],
                "contexts": ["уведомление за 30 дней", "неполное ограничение", "запрещённые услуги", "перерасчёт за период отключения", "восстановление"]
            },
            "ошибка начислений": {
                "synonyms": ["переплата", "неправильный расчёт", "завышенный тариф", "дублирование платежей", "техническая ошибка"],
                "norm_refs": ["ПП РФ №354, п. 95", "ЖК РФ, ст. 157"],
                "contexts": ["акт сверки", "жалоба в УК", "перерасчёт", "возврат излишне уплаченного", "срок исковой давности"]
            },
            "заявление на перерасчет": {
                "synonyms": ["заявление о перерасчёте", "ходатайство", "требование", "запрос перерасчёта", "документы для перерасчета"],
                "norm_refs": ["ПП РФ №354, п. 91", "ЖК РФ, ст. 157"],
                "contexts": ["письменная форма", "срок подачи (30 дней)", "приложение документов", "регистрация входящей корреспонденции", "срок рассмотрения (5 дней)"]
            },
            "формула перерасчета": {
                "synonyms": ["расчёт перерасчёта", "формула возврата", "математический расчёт", "пример расчёта", "расчёт по Приложению 2"],
                "norm_refs": ["ПП РФ №354, Приложение 2, формула 1", "ПП РФ №354, п. 90"],
                "contexts": ["по временному отсутствию", "по некачественной услуге", "по ошибке начисления", "примеры расчётов", "калькуляторы"]
            },
            "исключение из перерасчета": {
                "synonyms": ["не подлежит перерасчёту", "исключения", "отопление", "одн", "кру", "содержание общего имущества"],
                "norm_refs": ["ПП РФ №354, п. 86(2)", "ПП РФ №354, п. 90(2)"],
                "contexts": ["отопление в отопительный период", "услуги на общедомовые нужды", "содержание имущества", "почему не пересчитывают", "судебная практика"]
            },
            "техническая невозможность": {
                "synonyms": ["невозможность установки", "акт обследования", "отсутствие места", "ветхое состояние труб", "отказ в установке"],
                "norm_refs": ["ПП РФ №354, п. 85", "Приказ Минстроя №XXX"],
                "contexts": ["процедура оформления", "состав комиссии", "подписание акта", "начисление по нормативу без коэффициента", "обжалование акта"]
            },
            "отопление": {
                "synonyms": ["отопительный сезон", "температура в квартире", "холодно", "не греет", "радиатор"],
                "norm_refs": ["ПП РФ №354, п. 54(2)", "СанПиН 1.2.3685-21, п. 9.2"],
                "contexts": ["норма +18°C", "замер температуры", "акт", "перерасчёт при нарушении", "исключение из перерасчёта при временном отсутствии"]
            },
            "одн": {
                "synonyms": ["общедомовые нужды", "кру на сои", "коммунальный ресурс на содержание общего имущества", "расчёт одн"],
                "norm_refs": ["ПП РФ №354, раздел 9", "ПП РФ №491"],
                "contexts": ["расчёт по нормативу", "исключение из перерасчёта", "жалобы на завышение", "акт обследования", "судебная практика"]
            },
            "сроки перерасчета": {
                "synonyms": ["сроки рассмотрения", "срок перерасчёта", "когда вернут деньги", "срок возврата", "срок исполнения"],
                "norm_refs": ["ПП РФ №354, п. 91(5)", "ЖК РФ, ст. 157"],
                "contexts": ["5 рабочих дней на рассмотрение", "30 дней на возврат средств", "начисление в следующем периоде", "штрафы за нарушение сроков"]
            },
            "судебная практика по перерасчетам": {
                "synonyms": ["оспаривание отказа в перерасчёте", "взыскание переплаты", "позиция вс рф по перерасчётам", "судебные споры с ук"],
                "norm_refs": ["ГПК РФ, ст. 131", "КАС РФ, ст. 218"],
                "contexts": ["основания для удовлетворения", "доказательства отсутствия/нарушения", "сроки исковой давности", "моральный вред", "госпошлина"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "roscomnadzor.ru", "vsrf.ru", "gjirf.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".vsrf.ru", ".gjirf.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ПП РФ 354 перерасчет")
        queries.append(f"{query} ПП РФ 354 п 86 временная отсутствие")
        queries.append(f"{query} судебная практика по перерасчету за отопление")
        queries.append(f"{query} формула перерасчета при временном отсутствии")
        queries.append(f"{query} документы для перерасчета за командировку")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Перерасчёты ЖКУ
        Формирует системный промт:
        - Фокус: условия, формулы, сроки, документы по перерасчётам коммунальных услуг
        - Жёсткая структура, ссылки на нормативы и судебную практику
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        # Проверка на запрос о пени
        penalty_keywords = ["пени", "неустойка", "штраф за просрочку", "ставка цб", "9.5%", "ключевая ставка"]
        should_calculate_penalty = any(kw in summary.lower() for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — экспертный ИИ-ассистент по перерасчётам коммунальных услуг (ЖКУ). "
            "Отвечай строго по нормативам, без выдуманных данных, используя только контекст, веб-результаты и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если информации нет — отвечай: 'Недостаточно данных для точного ответа.'\n"
            "2. Обязательно указывай ссылки на нормативные акты.\n"
            "3. Структура: краткий вывод → нормативы → пошаговая инструкция → судебная практика.\n"
            "4. Формулы пени включай только при упоминании пени.\n"
            "5. Приоритет: ПП РФ №354 > ЖК РФ > ПП РФ №491 > судебная практика.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "--- Основной ответ ---\n"
            "Краткий вывод: [положен ли перерасчёт, что делать, какие документы нужны]\n"
            "Нормативное обоснование: [пункты ПП РФ №354, статьи ЖК РФ]\n"
            "Пошаговая инструкция:\n"
            "- Положен ли перерасчёт? (условия — ПП РФ №354, п.86, 90)\n"
            "- Как подать заявление? (сроки, форма, документы — ПП РФ №354, п.91)\n"
            "- Как рассчитывается сумма? (формула из Приложения 2 — ПП РФ №354)\n"
            "- Исключения? (отопление, ОДН — ПП РФ №354, п.86(2))\n"
            "- Сроки рассмотрения: 5 дней на проверку, 30 дней на возврат — ПП РФ №354, п.91(5)\n\n"
            "Судебная практика:\n"
            "- Определение ВС РФ №XXX-ЭСXX-XXXX — краткая позиция суда\n"
            "Если судебных решений нет: 'Судебная практика по данному вопросу в базе отсутствует'.\n\n"
            "Ключевые нормативные акты:\n"
            "- ПП РФ №354 «О предоставлении коммунальных услуг…» (п.86, 90, 91; Приложение 2)\n"
            "- ЖК РФ, ст.157 — перерасчёт и возврат\n"
            "- ПП РФ №491 — содержание общего имущества\n"
            "- СанПиН 1.2.3685-21 — параметры качества услуг\n"
        )
    
        # --- Динамический блок: расчет пени ---
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчёт пени (если упомянут):**\n"
                "- Формула: Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Основание: [ЖК РФ, ст.155.1], [ФЗ №44-ФЗ], [ПП РФ №329]\n"
                "- Ограничение: не более 9.5% годовых до 2027 года\n"
                "- Пример: 10 000 руб., просрочка 30 дней → 95 руб.\n"
                "- Начало начисления: с 31-го дня после окончания срока оплаты\n"
            )
    
        system_prompt += f"{self.get_role_instruction(role)}"
    
        # --- Формат для QVikhr / LLaMA-3 ---
        prompt_formatted = (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )
    
        return prompt_formatted

class CommonPropertyAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Управление Общим Имуществом", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "общее имущество": {
                "synonyms": ["oi", "общедомовое имущество", "ои", "имущество мкд", "коллективная собственность"],
                "norm_refs": ["ЖК РФ, ст. 36", "ПП РФ №491, Приложение 1"],
                "contexts": ["состав", "право собственности", "обязанности по содержанию", "использование", "распоряжение"]
            },
            "содержание ои": {
                "synonyms": ["содержание общего имущества", "управление ои", "обслуживание ои", "техническое обслуживание", "эксплуатация ои"],
                "norm_refs": ["ЖК РФ, ст. 154", "ПП РФ №491, п. 10-12"],
                "contexts": ["перечень работ", "тариф", "обязанности УК", "качество", "жалобы", "снижение платы"]
            },
            "ремонт ои": {
                "synonyms": ["текущий ремонт", "капитальный ремонт ои", "восстановление ои", "ремонт подвала", "ремонт крыши", "ремонт фасада"],
                "norm_refs": ["ЖК РФ, ст. 161", "ПП РФ №491, п. 12"],
                "contexts": ["периодичность", "финансирование", "акты приёмки", "гарантийный срок", "ответственность подрядчика"]
            },
            "благоустройство": {
                "synonyms": ["благоустройство придомовой территории", "озеленение", "уборка двора", "освещение", "детские площадки", "парковки"],
                "norm_refs": ["ПП РФ №491, п. 12(1)", "Правила благоустройства муниципалитета"],
                "contexts": ["обязанности УК", "периодичность", "стандарты", "жалобы", "фотоотчёты", "санитарные нормы"]
            },
            "одн": {
                "synonyms": ["общедомовые нужды", "кру на сои", "коммунальный ресурс на содержание общего имущества", "расходы на одн", "формула одн"],
                "norm_refs": ["ПП РФ №354, раздел 9", "ПП РФ №491, п. 20"],
                "contexts": ["расчёт по нормативу", "расчёт по показаниям ОДПУ", "исключение из перерасчёта", "жалобы на завышение", "судебная практика"]
            },
            "одпу": {
                "synonyms": ["коллективный счетчик", "общедомовой прибор учета", "домовой счётчик", "узел учёта", "вводной счётчик"],
                "norm_refs": ["ПП РФ №354, п. 40", "ФЗ №261-ФЗ, ст. 13"],
                "contexts": ["обязательность установки", "поверка", "передача показаний", "расчёт ОДН", "ответственность за неисправность"]
            },
            "снижение платы за содержание": {
                "synonyms": ["некачественное содержание", "акт нарушения качества", "уменьшение оплаты", "не убирают", "не ремонтируют", "антисанитария"],
                "norm_refs": ["ЖК РФ, ст. 156", "ПП РФ №354, п. 106"],
                "contexts": ["акт о нарушении", "срок устранения", "расчёт снижения платы", "жалоба в УК/ГЖИ", "судебное взыскание"]
            },
            "ежегодный перерасчет ои": {
                "synonyms": ["фактическое потребление ои", "корректировка платы за ои", "годовой перерасчёт", "сверка расходов", "отчёт о расходовании"],
                "norm_refs": ["ПП РФ №491, п. 32", "ЖК РФ, ст. 157"],
                "contexts": ["сроки (до 1 апреля)", "основание (акты, счётчики)", "расчёт по формуле", "возврат/доплата", "публикация в ГИС ЖКХ"]
            },
            "работы на ои": {
                "synonyms": ["перечень работ", "техническое обслуживание", "план работ", "график ремонта", "устранение аварий", "текущий ремонт"],
                "norm_refs": ["ПП РФ №491, п. 10-12", "Правила технической эксплуатации ЖКХ"],
                "contexts": ["обязательные работы", "периодичность", "качество", "приёмка", "акты", "ответственность УК"]
            },
            "состав общего имущества": {
                "synonyms": ["что входит в ои", "элементы ои", "перечень ои", "крыша, подвал, стены", "лифт, стояки, чердак"],
                "norm_refs": ["ЖК РФ, ст. 36", "ПП РФ №491, Приложение 1"],
                "contexts": ["законодательный перечень", "техническая документация", "БТИ", "акт разграничения", "судебные споры"]
            },
            "благоустройство придомовой территории": {
                "synonyms": ["двор", "газон", "тротуар", "парковка", "освещение", "детская площадка", "мусорные контейнеры"],
                "norm_refs": ["ПП РФ №491, п. 12(1)", "СанПиН 1.2.3685-21"],
                "contexts": ["обязанности УК", "стандарты", "периодичность уборки", "освещённость", "безопасность", "жалобы"]
            },
            "ремонт подвала": {
                "synonyms": ["ремонт цокольного этажа", "восстановление подвала", "гидроизоляция", "вентиляция подвала", "электрощиты"],
                "norm_refs": ["ПП РФ №491, п. 12", "Правила технической эксплуатации ЖКХ"],
                "contexts": ["текущий/капитальный ремонт", "финансирование", "акт обследования", "приёмка", "гарантия", "жалобы на затопление"]
            },
            "ответственность ук": {
                "synonyms": ["неисполнение обязанностей", "халатность", "игнорирование жалоб", "штрафы", "взыскание убытков", "моральный вред"],
                "norm_refs": ["ЖК РФ, ст. 161", "ГК РФ, ст. 1064"],
                "contexts": ["акты нарушений", "жалобы в ГЖИ", "судебные иски", "предписания", "дисквалификация", "отзыв лицензии"]
            },
            "жалоба на содержание ои": {
                "synonyms": ["акт о нарушении", "претензия ук", "обращение в гжи", "проверка ук", "фото как доказательство", "электронная жалоба"],
                "norm_refs": ["ЖК РФ, ст. 161", "ФЗ №59-ФЗ, ст. 12"],
                "contexts": ["срок ответа (30 дней)", "обязательность рассмотрения", "предписание", "внеплановая проверка", "обжалование"]
            },
            "судебная практика по ои": {
                "synonyms": ["оспаривание платы за ои", "взыскание убытков за некачественное содержание", "позиция вс рф по одн", "судебные споры с ук"],
                "norm_refs": ["ГПК РФ, ст. 131", "КАС РФ, ст. 218"],
                "contexts": ["основания для снижения платы", "доказательства нарушений", "расчёт убытков", "моральный вред", "госпошлина"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "roscomnadzor.ru", "vsrf.ru", "gjirf.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".vsrf.ru", ".gjirf.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ПП РФ 491 общее имущество")
        queries.append(f"{query} ПП РФ 354 расчет одн")
        queries.append(f"{query} судебная практика по снижению платы за содержание ои")
        queries.append(f"{query} состав общего имущества МКД ЖК РФ ст 36")
        queries.append(f"{query} ежегодный перерасчет за ои ПП РФ 491 п 32")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Управление общим имуществом
        Формирует промт:
        - Фокус: состав, плата, ОДН, перерасчёт, снижение платы
        - Строгая структура, ссылки на нормативы, судебная практика
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        # Проверка на запрос о пени
        penalty_keywords = ["пени", "неустойка", "штраф за просрочку", "ставка цб", "9.5%", "ключевая ставка"]
        should_calculate_penalty = any(kw in summary.lower() for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — экспертный ИИ-ассистент по управлению общим имуществом в многоквартирных домах (МКД). "
            "Отвечай строго по нормативам, без выдуманных данных, используя только контекст, веб-результаты и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если информации нет — отвечай: 'Недостаточно данных для точного ответа.'\n"
            "2. Обязательно указывай ссылки на нормативные акты.\n"
            "3. Структура: краткий вывод → нормативы → пошаговая инструкция → судебная практика.\n"
            "4. Формулы пени включай только при упоминании пени.\n"
            "5. Приоритет: ЖК РФ > ПП РФ №491 > ПП РФ №354 > судебная практика.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "--- Основной ответ ---\n"
            "Краткий вывод: [что входит в ОИ, положен ли перерасчёт, как снизить плату]\n"
            "Нормативное обоснование: [статьи ЖК РФ, пункты ПП РФ]\n"
            "Пошаговая инструкция:\n"
            "- Состав общего имущества (крыши, стены, лифты, подвалы — ЖК РФ, ст.36; ПП РФ №491, Приложение 1)\n"
            "- Расчёт платы за содержание ОИ (тариф, утверждённый ОСС — ЖК РФ, ст.156)\n"
            "- Расчёт ОДН (по нормативу или ОДПУ — ПП РФ №354, раздел 9)\n"
            "- Снижение платы за некачественные услуги (акт, заявление — ПП РФ №354, п.106)\n"
            "- Ежегодный перерасчёт (до 1 апреля, по факту — ПП РФ №491, п.32)\n\n"
            "Судебная практика:\n"
            "- Определение ВС РФ №XXX-ЭСXX-XXXX — краткая позиция суда\n"
            "Если судебных решений нет: 'Судебная практика по данному вопросу в базе отсутствует'.\n\n"
            "Ключевые нормативные акты:\n"
            "- ЖК РФ (ст.36, ст.154-158 — состав ОИ, плата, перерасчёт, ответственность)\n"
            "- ПП РФ №491 «Об утверждении Правил содержания ОИ…»\n"
            "- ПП РФ №354 (раздел 9 — ОДН, п.106 — снижение платы)\n"
            "- СанПиН 1.2.3685-21 (санитарные нормы содержания территорий)\n"
        )
    
        # --- Блок расчёта пени ---
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчёт пени (если упомянут):**\n"
                "- Формула: Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Основание: [ЖК РФ, ст.155.1], [ФЗ №44-ФЗ], [ПП РФ №329]\n"
                "- Ограничение: не более 9.5% годовых до 2027 года\n"
                "- Пример: 10 000 руб., просрочка 30 дней → 95 руб.\n"
                "- Начало начисления: с 31-го дня после окончания срока оплаты\n"
            )
    
        system_prompt += f"{self.get_role_instruction(role)}"
    
        # --- Формат для QVikhr / LLaMA ---
        prompt_formatted = (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )
    
        return prompt_formatted

class DisputeResolutionAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Разрешение Споров с УК/РСО", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "спор с ук": {
                "synonyms": ["конфликт с ук", "разногласия с управляющей компанией", "жалоба на ук", "претензия к ук", "неисполнение обязательств"],
                "norm_refs": ["ЖК РФ, ст. 161-162", "ГК РФ, ст. 309"],
                "contexts": ["качество услуг", "начисления", "отказ в перерасчёте", "игнорирование заявлений", "нарушение договора"]
            },
            "спор с рсо": {
                "synonyms": ["конфликт с рсо", "разногласия с ресурсоснабжающей организацией", "жалоба на рсо", "претензия к рсо", "ошибки в начислениях"],
                "norm_refs": ["ЖК РФ, ст. 156-157", "ПП РФ №354, раздел 10"],
                "contexts": ["некачественная услуга", "неверные показания", "отказ в акте сверки", "незаконные начисления", "ответственность рсо"]
            },
            "досудебное урегулирование": {
                "synonyms": ["претензионный порядок", "обязательная претензия", "досудебка", "попытка урегулировать", "претензия до суда"],
                "norm_refs": ["ЖК РФ, ст. 162", "ГК РФ, ст. 452"],
                "contexts": ["обязательность для ЖКХ", "срок 30 дней", "регистрация входящей корреспонденции", "подтверждение вручения", "последствия игнорирования"]
            },
            "жалоба в гжи": {
                "synonyms": ["обращение в жилинспекцию", "проверка гжи", "предписание ук", "жалоба на ук в гжи", "внеплановая проверка"],
                "norm_refs": ["ЖК РФ, ст. 20", "ФЗ №294-ФЗ, ст. 10"],
                "contexts": ["образец жалобы", "срок рассмотрения 30 дней", "акт проверки", "обжалование предписания", "штрафы для УК"]
            },
            "жалоба в роспотребнадзор": {
                "synonyms": ["обращение в роспотребнадзор", "проверка роспотребнадзора", "санитарные нормы", "качество услуг", "замер параметров"],
                "norm_refs": ["ФЗ №52-ФЗ", "СанПиН 1.2.3685-21"],
                "contexts": ["замеры температуры/давления", "акт санитарной проверки", "предписание", "ответ в течение 30 дней", "административная ответственность"]
            },
            "образец претензии": {
                "synonyms": ["шаблон претензии", "форма претензии", "заявление-претензия", "требование к ук", "досудебная претензия"],
                "norm_refs": ["ЖК РФ, ст. 162", "ГК РФ, ст. 452"],
                "contexts": ["реквизиты", "описание нарушения", "требование", "срок исполнения", "приложения", "регистрация"]
            },
            "сроки подачи иска": {
                "synonyms": ["исковая давность", "срок исковой давности", "пропущенный срок", "восстановление срока", "3 года"],
                "norm_refs": ["ГК РФ, ст. 196", "ГК РФ, ст. 200"],
                "contexts": ["3 года для жилищных споров", "начало течения", "приостановление", "восстановление по уважительным причинам", "судебная практика"]
            },
            "доказательства в жкх": {
                "synonyms": ["акты", "фото", "видео", "переписка", "квитанции", "показания свидетелей", "нотариальные документы", "экспертиза"],
                "norm_refs": ["ГПК РФ, ст. 67", "ФЗ №446-ФЗ"],
                "contexts": ["юридическая сила", "нотариальное заверение", "независимая экспертиза", "электронные доказательства", "акт в одностороннем порядке"]
            },
            "недобросовестность ук": {
                "synonyms": ["злоупотребление", "обман", "уклонение от обязанностей", "игнорирование жалоб", "систематические нарушения"],
                "norm_refs": ["ГК РФ, ст. 10", "ЖК РФ, ст. 161"],
                "contexts": ["судебная практика", "моральный вред", "дисквалификация", "отзыв лицензии", "взыскание убытков"]
            },
            "нарушение сроков": {
                "synonyms": ["просрочка", "не уложились в срок", "задержка", "не выполнили вовремя", "нарушение сроков устранения"],
                "norm_refs": ["ПП РФ №354, п. 59", "ЖК РФ, ст. 162"],
                "contexts": ["срок устранения нарушений", "срок ответа на претензию", "срок проведения проверки", "неустойка за просрочку", "жалоба в контролирующие органы"]
            },
            "неисполнение обязанностей": {
                "synonyms": ["уклонение от обязанностей", "не выполняет работу", "игнорирует заявки", "не ремонтирует", "не убирает"],
                "norm_refs": ["ЖК РФ, ст. 161", "ПП РФ №491, п. 12"],
                "contexts": ["акты нарушений", "жалобы", "предписания", "штрафы", "судебные иски", "расторжение договора"]
            },
            "нарушение качества услуг": {
                "synonyms": ["некачественное отопление", "слабый напор воды", "антисанитария", "неубранный подъезд", "неисправный лифт"],
                "norm_refs": ["ПП РФ №354, раздел 6", "СанПиН 1.2.3685-21"],
                "contexts": ["замер параметров", "акт о нарушении", "перерасчёт", "жалоба", "снижение платы", "судебная практика"]
            },
            "определение вс рф": {
                "synonyms": ["судебная практика вс рф", "позиция верховного суда", "разъяснения вс рф", "обзор практики", "постановление пленума"],
                "norm_refs": ["ГПК РФ, ст. 390", "КАС РФ, ст. 218"],
                "contexts": ["обязательность для нижестоящих судов", "единая практика", "толкование норм", "прецеденты", "цитирование в исках"]
            },
            "жалоба на отказ в перерасчёте": {
                "synonyms": ["отказ ук в перерасчёте", "отказ рсо в перерасчёте", "не признали отсутствие", "отказали по акту", "оспаривание отказа"],
                "norm_refs": ["ПП РФ №354, п. 91", "ЖК РФ, ст. 157"],
                "contexts": ["документы для обжалования", "жалоба в ГЖИ", "исковое заявление", "расчёт по формуле", "судебная практика"]
            },
            "судебная практика по жкх": {
                "synonyms": ["судебные споры с ук", "практика по жилищным делам", "решения судов по жкх", "обзоры практики", "позиция судов"],
                "norm_refs": ["ГПК РФ, ст. 131", "КАС РФ, ст. 218"],
                "contexts": ["основания для удовлетворения", "доказательства", "сроки", "моральный вред", "госпошлина", "исполнение решений"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "proc.gov.ru", "vsrf.ru", "sudrf.ru", "kad.arbitr.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".vsrf.ru", ".sudrf.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ЖК РФ ст 162 претензия УК")
        queries.append(f"{query} ПП РФ 354 перерасчет отказ")
        queries.append(f"{query} судебная практика по спорам с УК")
        queries.append(f"{query} образец претензии в УК по ЖКХ")
        queries.append(f"{query} жалоба в ГЖИ на отказ в перерасчёте")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Разрешение споров с УК/РСО
        Формирует промт:
        - Фокус: досудебное урегулирование, претензии, доказательства, иск, подсудность, сроки исковой давности
        - Строгая структура, ссылки на нормативы, судебная практика
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        # Проверка на запрос о пени
        penalty_keywords = ["пени", "неустойка", "штраф за просрочку", "ставка цб", "9.5%", "ключевая ставка"]
        should_calculate_penalty = any(kw in summary.lower() for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — экспертный ИИ-ассистент по жилищным спорам.\n"
            "Отвечай строго по нормативам, без выдуманных данных, используя только контекст, веб-результаты и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если информации нет — отвечай: 'Недостаточно данных для точного ответа.'\n"
            "2. Обязательно указывай ссылки на нормативные акты.\n"
            "3. Структура: краткий вывод → нормативы → пошаговая инструкция → судебная практика.\n"
            "4. Формулы пени включай только при упоминании пени.\n"
            "5. Приоритет: ЖК РФ > ГК РФ > ПП РФ > ФЗ > судебная практика.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "--- Основной ответ ---\n"
            "Краткий вывод: [что делать, куда обращаться, шансы на успех]\n"
            "Нормативное обоснование: [статьи ЖК РФ, ГК РФ, ПП РФ, ФЗ]\n"
            "Пошаговая инструкция:\n"
            "- Досудебное урегулирование: как составить и направить претензию (ЖК РФ, ст.162)\n"
            "- Сбор доказательств: акты, фото, переписка, свидетели (ГПК РФ, ст.67)\n"
            "- Подача жалобы: ГЖИ, Роспотребнадзор, прокуратура (ФЗ №59-ФЗ, ст.12)\n"
            "- Подача иска: подсудность, госпошлина, приложения (ГПК РФ, ст.131)\n"
            "- Сроки: исковой давности — 3 года (ГК РФ, ст.196), рассмотрение претензии — 30 дней (ЖК РФ, ст.162)\n\n"
            "Судебная практика:\n"
            "- Определение ВС РФ №XXX-ЭСXX-XXXX — краткая позиция суда\n"
            "Если судебных решений нет: 'Судебная практика по данному вопросу в базе отсутствует'.\n\n"
            "Ключевые нормативные акты:\n"
            "- ЖК РФ (ст.155-162 — обязательства, претензии, ответственность УК)\n"
            "- Гражданский кодекс РФ (ст.196 — исковая давность, ст.309 — исполнение обязательств)\n"
            "- ПП РФ №354 (порядок расчётов и качества услуг)\n"
            "- ПП РФ №491 (содержание общего имущества)\n"
            "- ФЗ №59-ФЗ «О порядке рассмотрения обращений граждан»\n"
            "- ФЗ №294-ФЗ «О защите прав при госконтроле»\n"
        )
    
        # --- Блок расчёта пени ---
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчёт пени (если упомянут):**\n"
                "- Формула: Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Основание: [ЖК РФ, ст.155.1], [ФЗ №44-ФЗ], [ПП РФ №329]\n"
                "- Ограничение: не более 9.5% годовых до 2027 года\n"
                "- Пример: 10 000 руб., просрочка 30 дней → 95 руб.\n"
                "- Начало начисления: с 31-го дня после окончания срока оплаты\n"
            )
    
        system_prompt += f"{self.get_role_instruction(role)}"
    
        # --- Формат для QVikhr / LLaMA ---
        prompt_formatted = (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )
    
        return prompt_formatted

class ProceduralAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Процедурный Агент", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "заявление": {
                "synonyms": ["заявка", "ходатайство", "обращение", "письмо", "запрос", "требование"],
                "norm_refs": ["ФЗ №59-ФЗ, ст. 12", "ЖК РФ, ст. 162"],
                "contexts": ["в УК", "в ГЖИ", "в суд", "на перерасчёт", "на вызов мастера", "на получение информации"]
            },
            "акт": {
                "synonyms": ["акт осмотра", "акт обследования", "акт проверки", "акт о нарушении", "односторонний акт", "акт приёма-передачи"],
                "norm_refs": ["ПП РФ №354, п. 99", "ПП РФ №491, п. 10"],
                "contexts": ["состав комиссии", "обязательные реквизиты", "срок подписания", "фото/видео как приложение", "использование в суде"]
            },
            "претензия": {
                "synonyms": ["досудебная претензия", "требование", "жалоба", "письменное обращение", "образец претензии"],
                "norm_refs": ["ЖК РФ, ст. 162", "ГК РФ, ст. 452"],
                "contexts": ["обязательность", "срок ответа 30 дней", "регистрация входящей корреспонденции", "последствия игнорирования", "приложения"]
            },
            "образец / форма": {
                "synonyms": ["шаблон", "бланк", "форма документа", "типовой образец", "официальная форма"],
                "norm_refs": ["ПП РФ №354, Приложения", "ПП РФ №491, Приложения", "Приказы Минстроя"],
                "contexts": ["где скачать", "обязательность использования", "электронный формат", "подпись", "печать", "нотариальное заверение"]
            },
            "пошаговая инструкция": {
                "synonyms": ["шаги", "порядок действий", "алгоритм", "инструкция", "процедура", "регламент"],
                "norm_refs": [],
                "contexts": ["составление документа", "подача", "регистрация", "получение ответа", "обжалование", "судебное оспаривание"]
            },
            "документы": {
                "synonyms": ["приложения", "справки", "копии", "выписки", "доказательства", "подтверждающие документы"],
                "norm_refs": ["ГПК РФ, ст. 67", "ФЗ №59-ФЗ, ст. 12"],
                "contexts": ["перечень для каждого типа заявления", "нотариальное заверение", "апостиль", "электронные копии", "сроки действия"]
            },
            "заявление в ук": {
                "synonyms": ["обращение в управляющую компанию", "запрос в ук", "письмо в ук", "требование к ук"],
                "norm_refs": ["ЖК РФ, ст. 162", "ПП РФ №354, п. 95"],
                "contexts": ["регистрация", "срок ответа 10-30 дней", "обязательность предоставления информации", "жалоба при отсутствии ответа"]
            },
            "акт осмотра": {
                "synonyms": ["акт проверки качества", "акт о заливе", "акт о протечке", "акт о ненадлежащем качестве"],
                "norm_refs": ["ПП РФ №354, п. 99", "ЖК РФ, ст. 157"],
                "contexts": ["состав комиссии", "фото/видео", "подписание", "односторонний акт", "использование для перерасчёта/взыскания"]
            },
            "акт обследования": {
                "synonyms": ["акт технического состояния", "акт о невозможности установки", "акт о дефектах", "акт осмотра общего имущества"],
                "norm_refs": ["ПП РФ №354, п. 85", "ПП РФ №491, п. 10"],
                "contexts": ["техническая невозможность установки ИПУ", "состояние общего имущества", "основание для капремонта", "жалоба в ГЖИ"]
            },
            "претензия на некачественную услугу": {
                "synonyms": ["жалоба на качество", "требование о перерасчёте", "заявление о снижении платы", "претензия по качеству"],
                "norm_refs": ["ЖК РФ, ст. 156", "ПП РФ №354, п. 106"],
                "contexts": ["акт о нарушении", "расчёт снижения платы", "срок устранения", "обращение в контролирующие органы", "суд"]
            },
            "заявка на вызов мастера": {
                "synonyms": ["вызов сантехника", "вызов электрика", "заявка в аварийку", "заявка на ремонт", "заявка на устранение неисправности"],
                "norm_refs": ["ПП РФ №354, п. 98", "Правила технической эксплуатации ЖКХ"],
                "contexts": ["способы подачи (телефон, личный кабинет, ГИС ЖКХ)", "срок реагирования", "регистрационный номер", "статус выполнения"]
            },
            "уведомление": {
                "synonyms": ["извещение", "оповещение", "сообщение", "предупреждение", "уведомление о начале работ", "уведомление о проведении собрания"],
                "norm_refs": ["ЖК РФ, ст. 45", "ПП РФ №354, п. 98(5)"],
                "contexts": ["сроки (не менее 10 дней)", "способы (лично, почтой, в ГИС ЖКХ)", "обязательность", "последствия неуведомления"]
            },
            "предписание": {
                "synonyms": ["предписание ук", "предписание гжи", "предписание роспотребнадзора", "постановление", "распоряжение", "приказ"],
                "norm_refs": ["ФЗ №294-ФЗ, ст. 16", "ЖК РФ, ст. 20"],
                "contexts": ["обязательность исполнения", "срок исполнения", "штраф за неисполнение", "обжалование", "приоставление исполнения"]
            },
            "судебная практика по документам": {
                "synonyms": ["оспаривание отказа в приёме", "недействительность акта", "обязательность претензии", "позиция вс рф по формам"],
                "norm_refs": ["ГПК РФ, ст. 131", "КАС РФ, ст. 218"],
                "contexts": ["основания для признания документа недействительным", "доказательства вручения", "сроки", "роль прокурора", "последствия"]
            },
            "сроки рассмотрения": {
                "synonyms": ["срок ответа", "срок исполнения", "срок регистрации", "срок устранения", "срок давности"],
                "norm_refs": ["ФЗ №59-ФЗ, ст. 12", "ЖК РФ, ст. 162", "ГК РФ, ст. 196"],
                "contexts": ["10 дней — информация от УК", "30 дней — претензии и жалобы", "5 дней — перерасчёт", "3 года — исковая давность"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "roscomnadzor.ru", "vsrf.ru", "gjirf.ru", "fns.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".vsrf.ru", ".gjirf.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ПП РФ 354 образец акта")
        queries.append(f"{query} ЖК РФ ст 162 образец претензии")
        queries.append(f"{query} судебная практика по оформлению актов ЖКХ")
        queries.append(f"{query} как составить заявление на перерасчет образец")
        queries.append(f"{query} форма уведомления о проведении ОСС")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Процедурный агент
        Формирует промт:
        - Фокус: оформление процедурных документов в ЖКХ — акты, заявки, претензии, формы, сроки
        - Строгая структура, ссылки на нормативы, судебная практика
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        # Проверка на запрос о пени
        penalty_keywords = ["пени", "неустойка", "штраф за просрочку", "ставка цб", "9.5%", "ключевая ставка"]
        should_calculate_penalty = any(kw in summary.lower() for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — экспертный ИИ-ассистент по оформлению процедурных документов в сфере ЖКХ.\n"
            "Отвечай строго по нормативам, без выдуманных данных, используя только контекст, веб-результаты и обновления.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если информации нет — отвечай: 'Недостаточно данных для точного ответа.'\n"
            "2. Обязательно указывай ссылки на нормативные акты.\n"
            "3. Структура: краткий вывод → нормативы → пошаговая инструкция → судебная практика.\n"
            "4. Формулы пени включай только при упоминании пени.\n"
            "5. Приоритет: ЖК РФ > ПП РФ > ФЗ > судебная практика.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "--- Основной ответ ---\n"
            "Краткий вывод: [какой документ нужен, где взять образец, как подать]\n"
            "Нормативное обоснование: [статьи ЖК РФ, ПП РФ, ФЗ]\n"
            "Пошаговая инструкция:\n"
            "- Какие сведения должны быть в документе? (реквизиты, описание, требования — ПП РФ №354, Приложения)\n"
            "- Как правильно оформить? (подпись, печать, приложения — ЖК РФ, ст.162)\n"
            "- Куда и как подать? (лично, почтой, через ГИС ЖКХ — ФЗ №59-ФЗ, ст.12)\n"
            "- Сроки рассмотрения? (10 дней на информацию, 30 дней на претензии — ФЗ №59-ФЗ, ст.12)\n"
            "- Что делать при отказе или отсутствии ответа? (жалоба в ГЖИ, суд — ЖК РФ, ст.20)\n\n"
            "Судебная практика:\n"
            "- Определение ВС РФ №XXX-ЭСXX-XXXX — краткая позиция суда\n"
            "Если судебных решений нет: 'Судебная практика по данному вопросу в базе отсутствует'.\n\n"
            "Ключевые нормативные акты:\n"
            "- ЖК РФ (ст.45 — уведомления, ст.162 — претензии)\n"
            "- ПП РФ №354 (п.98-99 — акты, заявки; Приложения — формы документов)\n"
            "- ПП РФ №491 (п.10 — акты по общему имуществу)\n"
            "- ФЗ №59-ФЗ «О порядке рассмотрения обращений граждан»\n"
            "- ФЗ №294-ФЗ «О защите прав при госконтроле»\n"
        )
    
        # --- Блок расчёта пени ---
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчёт пени (если упомянут):**\n"
                "- Формула: Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Основание: [ЖК РФ, ст.155.1], [ФЗ №44-ФЗ], [ПП РФ №329]\n"
                "- Ограничение: не более 9.5% годовых до 2027 года\n"
                "- Пример: 10 000 руб., просрочка 30 дней → 95 руб.\n"
                "- Начало начисления: с 31-го дня после окончания срока оплаты\n"
            )
    
        system_prompt += f"{self.get_role_instruction(role)}"
    
        # --- Формат для QVikhr / LLaMA ---
        prompt_formatted = (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )
    
        return prompt_formatted

class NPBAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Нормативно-Правовая База", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "жк рф": {
                "synonyms": ["жилищный кодекс", "жилищный кодекс рф", "жк", "жилищное законодательство", "жилищный кодекс россии"],
                "norm_refs": ["ЖК РФ"],
                "contexts": ["актуальная редакция", "последние изменения", "главы 4-9", "скачать полный текст", "применение на практике"]
            },
            "пп 354": {
                "synonyms": ["постановление правительства 354", "пп №354", "правила предоставления коммунальных услуг", "354 постановление"],
                "norm_refs": ["ПП РФ №354"],
                "contexts": ["расчёт платы", "перерасчёт", "качество услуг", "ответственность УК", "последняя редакция 2025"]
            },
            "пп 491": {
                "synonyms": ["постановление правительства 491", "пп №491", "правила содержания общего имущества", "491 постановление"],
                "norm_refs": ["ПП РФ №491"],
                "contexts": ["состав ОИ", "обязанности УК", "ремонт и обслуживание", "расчёт ОДН", "актуальная редакция"]
            },
            "пп 731": {
                "synonyms": ["постановление правительства 731", "пп №731", "стандартизация раскрытия информации", "731 постановление"],
                "norm_refs": ["ПП РФ №731"],
                "contexts": ["раскрытие информации", "обязанности УК", "ГИС ЖКХ", "формы отчётов", "сроки публикации"]
            },
            "фз 261": {
                "synonyms": ["федеральный закон 261", "фз об энергосбережении", "261-фз", "энергосбережение", "установка счётчиков"],
                "norm_refs": ["ФЗ №261-ФЗ"],
                "contexts": ["обязанность установки ИПУ", "энергоаудит", "энергосервис", "последние изменения", "сроки исполнения"]
            },
            "фз 59": {
                "synonyms": ["федеральный закон 59", "фз о порядке рассмотрения обращений", "59-фз", "жалобы граждан", "сроки ответа"],
                "norm_refs": ["ФЗ №59-ФЗ"],
                "contexts": ["30 дней на ответ", "письменная форма", "регистрация обращений", "жалобы на УК", "обжалование"]
            },
            "фз 294": {
                "synonyms": ["федеральный закон 294", "фз о защите прав при госконтроле", "294-фз", "проверки ук", "предписания"],
                "norm_refs": ["ФЗ №294-ФЗ"],
                "contexts": ["плановые/внеплановые проверки", "предписания", "жалобы на контролирующие органы", "сроки", "обжалование"]
            },
            "санпин": {
                "synonyms": ["санитарные правила", "санпин 1.2.3685-21", "гигиенические требования", "санитарные нормы", "качество услуг"],
                "norm_refs": ["СанПиН 1.2.3685-21"],
                "contexts": ["температура", "давление воды", "шум", "освещение", "антисанитария", "жалобы в Роспотребнадзор"]
            },
            "снип": {
                "synonyms": ["строительные нормы", "снипы", "строительные правила", "проектирование", "строительство", "эксплуатация"],
                "norm_refs": ["СП 50.13330.2012", "СП 60.13330.2016"],
                "contexts": ["тепловая защита", "отопление", "вентиляция", "благоустройство", "технические требования"]
            },
            "гост": {
                "synonyms": ["государственные стандарты", "госты", "технические регламенты", "качество материалов", "испытания"],
                "norm_refs": ["ГОСТ 30674-99", "ГОСТ Р 58237-2024"],
                "contexts": ["окна", "двери", "материалы", "безопасность", "сертификация", "требования к оборудованию"]
            },
            "постановление правительства": {
                "synonyms": ["пп рф", "постановление", "правительственное постановление", "нормативный акт правительства", "актуальные постановления"],
                "norm_refs": [],
                "contexts": ["пп №354", "пп №491", "пп №731", "пп №1149", "где найти", "официальный интернет-портал правовой информации"]
            },
            "федеральный закон": {
                "synonyms": ["фз", "федеральный закон рф", "закон", "нормативный акт", "жк рф", "фз №261", "фз №59"],
                "norm_refs": [],
                "contexts": ["где читать", "последняя редакция", "изменения", "официальный сайт", "консультантплюс", "гарант"]
            },
            "приказ минстроя": {
                "synonyms": ["приказ", "приказы министерства строительства", "формы отчётов", "методические рекомендации", "утверждённые формы"],
                "norm_refs": ["Приказ Минстроя №48/414", "Приказ Минстроя №74/пр"],
                "contexts": ["формы годовых отчётов", "порядок загрузки в ГИС ЖКХ", "актуальные приказы", "скачать", "регистрация"]
            },
            "письмо минстроя": {
                "synonyms": ["разъяснения минстроя", "письма", "официальные разъяснения", "толкование норм", "методические письма"],
                "norm_refs": [],
                "contexts": ["не нормативные, но авторитетные", "используются в судах", "толкование спорных положений", "примеры расчётов", "скачать с сайта"]
            },
            "актуальная редакция": {
                "synonyms": ["последняя редакция", "действующая редакция", "новая редакция", "изменения в закон", "вступил в силу"],
                "norm_refs": [],
                "contexts": ["дата вступления", "сайт pravo.gov.ru", "консультантплюс", "гарант", "официальный интернет-портал правовой информации"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "roscomnadzor.ru", "vsrf.ru", "gjirf.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".pravo.gov.ru", ".vsrf.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} официальный текст")
        queries.append(f"{query} последняя редакция 2025")
        queries.append(f"{query} pravo.gov.ru")
        queries.append(f"{query} консультантплюс гарант")
        queries.append(f"{query} вступил в силу когда")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Нормативно-Правовая База
        Формирует промт:
        - Фокус: поиск и актуализация нормативно-правовых актов ЖКХ
        - Строгая структура, ссылки на официальные источники, взаимосвязи, судебная практика
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        # Проверка на запрос о пени
        penalty_keywords = ["пени", "неустойка", "штраф за просрочку", "ставка цб", "9.5%", "ключевая ставка"]
        should_calculate_penalty = any(kw in summary.lower() for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — экспертный ИИ-ассистент по нормативно-правовой базе в ЖКХ.\n"
            "Отвечай строго по официальным источникам и предоставленному контексту.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. Если информации нет — отвечай: 'Недостаточно данных для точного ответа.'\n"
            "2. Обязательно указывай ссылки на нормативные акты.\n"
            "3. Структура: краткий вывод → ключевые положения → актуальность → практическое применение → взаимосвязи → судебная практика.\n"
            "4. Формулы пени включай только при упоминании пени.\n"
            "5. Приоритет: официальные источники (pravo.gov.ru, consultant.ru, garant.ru) > судебная практика.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "--- Основной ответ ---\n"
            "Краткий вывод: [где найти текст, последняя редакция, основные положения]\n"
            "Полный текст / Ключевые положения: [статьи, пункты, ссылки на официальные источники]\n"
            "Актуальность и вступление в силу:\n"
            "- Дата последней редакции: [Указать]\n"
            "- Дата вступления в силу: [Указать]\n"
            "- Проверка актуальности: [pravo.gov.ru, consultant.ru, garant.ru]\n"
            "Практическое применение:\n"
            "- Как применяется (примеры, типовые ситуации)\n"
            "- Какие документы регулирует (квитанции, акты, договоры)\n"
            "- Контролирующие органы (ГЖИ, Роспотребнадзор, ФАС)\n"
            "Взаимосвязи с другими актами:\n"
            "- Дополняющие или изменяющие акты\n"
            "- Акты, утрачивающие силу\n"
            "- Подзаконные акты, принятые на основе данного документа\n"
            "Судебная практика:\n"
            "- Определение ВС РФ №XXX-ЭСXX-XXXX — краткая позиция суда\n"
            "Если судебных решений нет: 'Судебная практика по данному вопросу в базе отсутствует'.\n"
            "Ключевые нормативные акты:\n"
            "- ЖК РФ (ст.154-162 — плата, перерасчёты, ответственность)\n"
            "- ПП РФ №354 (акты, формы, порядок расчётов)\n"
            "- ПП РФ №491 (содержание общего имущества)\n"
            "- СанПиН 1.2.3685-21 (санитарные нормы)\n"
            "- ФЗ №59-ФЗ, ФЗ №294-ФЗ (порядок обращений, госконтроль)\n"
        )
    
        # --- Блок расчёта пени ---
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчёт пени (если упомянут):**\n"
                "- Формула: Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Основание: [ЖК РФ, ст.155.1], [ФЗ №44-ФЗ], [ПП РФ №329]\n"
                "- Ограничение: не более 9.5% годовых до 2027 года\n"
                "- Пример: 10 000 руб., просрочка 30 дней → 95 руб.\n"
                "- Начало начисления: с 31-го дня после окончания срока оплаты\n"
            )
    
        system_prompt += f"{self.get_role_instruction(role)}"
    
        # --- Формат для QVikhr / LLaMA ---
        prompt_formatted = (
            "<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n"
            f"{system_prompt}<|eot_id|>"
        )
    
        return prompt_formatted

class IPUODPUAgent(RAGAgent):
    def __init__(self):
        # Расширенный и структурированный словарь с синонимами и контекстами
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Приборы Учета (ИПУ/ОДПУ)", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "ипу": {
                "synonyms": ["индивидуальный прибор учета", "квартирный счётчик", "счётчик в квартире", "умный счётчик", "телеметрический счётчик"],
                "norm_refs": ["ФЗ №261-ФЗ, ст. 13", "ПП РФ №354, п. 31"],
                "contexts": ["обязательность установки", "сроки монтажа", "поверка", "автоматическая передача", "расчёт по показаниям", "штрафы за отсутствие"]
            },
            "одпу": {
                "synonyms": ["общедомовой прибор учета", "домовой счётчик", "узел учёта", "коллективный счётчик", "вводной счётчик"],
                "norm_refs": ["ФЗ №261-ФЗ, ст. 13", "ПП РФ №354, п. 40"],
                "contexts": ["обязательность установки", "место установки", "поверка", "передача показаний", "расчёт платы", "ответственность за неисправность"]
            },
            "установка счетчика": {
                "synonyms": ["монтаж счётчика", "установка ипу/одпу", "подключение счётчика", "замена счётчика", "демонтаж счётчика"],
                "norm_refs": ["ПП РФ №354, п. 31(5)", "ФЗ №261-ФЗ, ст. 13"],
                "contexts": ["за чей счёт", "сроки", "согласование", "допуск", "акт ввода в эксплуатацию", "технические требования"]
            },
            "поверка счетчика": {
                "synonyms": ["калибровка", "метрологическая поверка", "межповерочный интервал", "дата последней поверки", "срок поверки", "истёк срок поверки"],
                "norm_refs": ["ФЗ №102-ФЗ", "ПП РФ №354, п. 81"],
                "contexts": ["периодичность", "кто проводит", "стоимость", "акт поверки", "начисление по нормативу при просрочке", "жалоба на РСО/УК"]
            },
            "опломбировка": {
                "synonyms": ["пломбировка", "опечатывание", "ввод в эксплуатацию", "допуск к эксплуатации", "отказ в опломбировке"],
                "norm_refs": ["ПП РФ №354, п. 31(3)", "Правила технической эксплуатации ЖКХ"],
                "contexts": ["обязанность УК/РСО", "сроки опломбировки (не более 15 дней)", "штрафы за отказ", "самостоятельная опломбировка запрещена", "акт ввода в эксплуатацию"]
            },
            "снятие показаний": {
                "synonyms": ["передача показаний", "отправка показаний", "предоставление показаний", "сроки передачи", "автоматическая передача"],
                "norm_refs": ["ПП РФ №354, п. 31(1)", "ФЗ №261-ФЗ, ст. 13(5)"],
                "contexts": ["с 23 по 25 число", "способы (лично, онлайн, через УК)", "последствия не передачи", "расчёт по среднему", "начисление по нормативу"]
            },
            "доступ к пусу": {
                "synonyms": ["доступ к счётчику", "допуск в квартиру", "проверка счётчика", "отказ в доступе", "не пускают в квартиру"],
                "norm_refs": ["ПП РФ №354, п. 31(4)", "ЖК РФ, ст. 36"],
                "contexts": ["обязанность собственника предоставить доступ", "предварительное уведомление", "штрафы за отказ", "начисление по нормативу", "акт об отказе"]
            },
            "отказ в поверке": {
                "synonyms": ["отказ в допуске к поверке", "не принимают счётчик", "признание непригодным", "дефект счётчика", "отказ в приёмке"],
                "norm_refs": ["ПП РФ №354, п. 81(12)", "ФЗ №102-ФЗ"],
                "contexts": ["основания для отказа", "акт обследования", "жалоба в Ростехнадзор", "установка нового счётчика", "начисление по нормативу с коэффициентом 1.5"]
            },
            "отказ в опломбировке": {
                "synonyms": ["не опломбировали", "отказали в допуске", "не приняли счётчик", "требуют замены", "технические требования"],
                "norm_refs": ["ПП РФ №354, п. 31(3)", "Правила технической эксплуатации ЖКХ"],
                "contexts": ["законные основания", "жалоба в ГЖИ", "установка за свой счёт", "судебное оспаривание", "начисление по нормативу"]
            },
            "техническая невозможность": {
                "synonyms": ["невозможность установки", "акт обследования", "отсутствие места", "ветхое состояние труб", "отказ в установке"],
                "norm_refs": ["ПП РФ №354, п. 85", "Приказ Минстроя №XXX"],
                "contexts": ["процедура оформления", "состав комиссии", "подписание акта", "начисление по нормативу без коэффициента", "обжалование акта"]
            },
            "автоматическая передача показаний": {
                "synonyms": ["умный счётчик", "телеметрия", "дистанционная передача", "интеграция с гис жкх", "интеллектуальные системы учёта"],
                "norm_refs": ["ФЗ №261-ФЗ, ст. 13(5)", "ПП РФ №354, п. 31(1)"],
                "contexts": ["обязательность с 2025 года", "совместимость", "стоимость установки", "передача без участия жильца", "защита данных"]
            },
            "дата последней поверки": {
                "synonyms": ["срок поверки", "межповерочный интервал", "дата следующей поверки", "паспорт счётчика", "свидетельство о поверке"],
                "norm_refs": ["ФЗ №102-ФЗ", "ПП РФ №354, п. 81"],
                "contexts": ["где посмотреть", "сроки для разных типов счётчиков", "ответственность за просрочку", "начисление по нормативу", "замена счётчика"]
            },
            "межповерочный интервал": {
                "synonyms": ["мпи", "срок действия поверки", "периодичность поверки", "интервал поверки", "срок между поверками"],
                "norm_refs": ["ФЗ №102-ФЗ, Приложение 2", "ПП РФ №354, п. 81"],
                "contexts": ["горячая вода — 4 года", "холодная вода — 6 лет", "тепло — 4 года", "газ — 10 лет", "электричество — 16 лет", "ответственность за просрочку"]
            },
            "начисление по нормативу": {
                "synonyms": ["расчёт по нормативу", "начисление без счётчика", "повышающий коэффициент", "коэффициент 1.5", "отсутствие ипу"],
                "norm_refs": ["ПП РФ №354, п. 42", "ПП РФ №354, п. 42(1)"],
                "contexts": ["условия применения", "расчёт по среднему", "повышающий коэффициент 1.5", "отказ в допуске", "истёк срок поверки", "техническая невозможность"]
            },
            "судебная практика по приборам учета": {
                "synonyms": ["оспаривание начислений", "отказ в опломбировке", "техническая невозможность", "позиция вс рф по ипу", "судебные споры с ук/рсо"],
                "norm_refs": ["ГПК РФ, ст. 131", "КАС РФ, ст. 218"],
                "contexts": ["основания для отмены начислений", "доказательства технической возможности", "сроки исковой давности", "моральный вред", "госпошлина"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "cbr.ru", "government.ru", "kremlin.ru", "rosstat.gov.ru", "minfin.gov.ru",
            "fas.gov.ru", "gji.ru", "rospotrebnadzor.ru", "rosreestr.gov.ru",
            "minstroyrf.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "roscomnadzor.ru", "rostec.ru", "vsrf.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".pravo.gov.ru", ".vsrf.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ФЗ 261-ФЗ приборы учета")
        queries.append(f"{query} ПП РФ 354 п 81 поверка счетчиков")
        queries.append(f"{query} судебная практика по отказу в опломбировке счетчика")
        queries.append(f"{query} межповерочный интервал для счетчиков воды")
        queries.append(f"{query} техническая невозможность установки ИПУ образец акта")
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Приборы учёта (ИПУ/ОДПУ)
        Формирует промт:
        - Фокус: установка, поверка, передача показаний, техническая невозможность
        - Строгая структура, ссылки на нормативные акты, судебная практика
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        # Проверка на запрос о пени
        penalty_keywords = ["пени", "неустойка", "штраф за просрочку", "ставка цб", "9.5%", "ключевая ставка"]
        should_calculate_penalty = any(kw in summary.lower() for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — экспертный ИИ-ассистент по приборам учёта (ИПУ/ОДПУ) в ЖКХ.\n"
            "Отвечай строго по официальным источникам и предоставленному контексту.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. НИКАКИХ ГАЛЛЮЦИНАЦИЙ: если информации нет — ответь: 'Недостаточно данных для точного ответа.'\n"
            "2. ОБЯЗАТЕЛЬНО указывай ссылки на нормативные акты.\n"
            "3. СТРУКТУРА: краткий вывод → нормативное обоснование → пошаговая инструкция → судебная практика.\n"
            "4. ФОРМУЛЫ ТОЛЬКО ПРИ ЗАПРОСЕ о пени.\n"
            "5. Приоритет: ФЗ №261-ФЗ > ПП РФ №354 > ФЗ №102-ФЗ > судебная практика.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "--- Основной ответ ---\n"
            "Краткий вывод: [что делать, кто отвечает, законность требований]\n"
            "Нормативное обоснование: [статьи ФЗ №261-ФЗ, ПП РФ №354, ФЗ №102-ФЗ]\n"
            "Пошаговая инструкция:\n"
            "- Обязаны ли устанавливать ИПУ/ОДПУ? (ФЗ №261-ФЗ, ст.13)\n"
            "- Порядок установки, замены, опломбировки (ПП РФ №354, п.31)\n"
            "- Порядок поверки, межповерочные интервалы (ПП РФ №354, п.81; ФЗ №102-ФЗ)\n"
            "- Передача показаний (ПП РФ №354, п.31(1))\n"
            "- Действия при отказе или технической невозможности (ПП РФ №354, п.85)\n"
            "Судебная практика:\n"
            "- Определение ВС РФ №XXX-ЭСXX-XXXX — краткая позиция суда\n"
            "Если судебных решений нет: 'Судебная практика по данному вопросу в базе отсутствует'.\n"
            "Ключевые нормативные акты:\n"
            "- ФЗ №261-ФЗ (ст.13 — установка ИПУ)\n"
            "- ПП РФ №354 (п.31, 81, 85 — установка, поверка, техническая невозможность)\n"
            "- ФЗ №102-ФЗ (поверка, межповерочные интервалы)\n"
            "- ПП РФ №491 (если ОДПУ касается общего имущества)\n"
        )
    
        # --- Блок расчёта пени ---
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчёт пени (если упомянут):**\n"
                "- Формула: Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Основание: [ЖК РФ, ст.155.1], [ФЗ №44-ФЗ], [ПП РФ №329]\n"
                "- Ограничение: не более 9.5% годовых до 2027 года\n"
                "- Пример: 10 000 руб., просрочка 30 дней → 95 руб.\n"
                "- Начало начисления: с 31-го дня после окончания срока оплаты\n"
            )
    
        system_prompt += f"{self.get_role_instruction(role)}"
    
        # --- Формат для LLaMA / QVikhr ---
        prompt_formatted = f"{system_prompt}"
    
        return prompt_formatted

class GISGKHAgent(RAGAgent):
    def __init__(self):
        # Строим семантическую карту терминов
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Госуслуги и ГИС ЖКХ", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "госуслуги": {
                "synonyms": ["портал госуслуг", "сайт госуслуг", "gosuslugi.ru", "госуслуги рф", "единый портал госуслуг"],
                "norm_refs": ["ФЗ №210-ФЗ", "ПП РФ №1191", "Приказ Минцифры №XXX"],
                "contexts": ["регистрация", "подтверждение личности", "электронная подпись", "мобильное приложение", "техподдержка", "жалобы на портал"]
            },
            "гис жкх": {
                "synonyms": ["гис жкх", "портал гис жкх", "dom.gosuslugi.ru", "государственная информационная система жкх", "гис жилищно-коммунального хозяйства"],
                "norm_refs": ["ФЗ №209-ФЗ", "ПП РФ №1131", "Приказ Минстроя №XXX"],
                "contexts": ["личный кабинет жильца", "личный кабинет УК", "отчёты УК", "тарифы ЖКХ", "состав общего имущества", "передача показаний", "жалобы на УК"]
            },
            "личный кабинет жильца": {
                "synonyms": ["личный кабинет на госуслугах", "кабинет собственника", "доступ к информации по дому", "личный аккаунт жильца", "интерфейс жильца в гис жкх"],
                "norm_refs": ["ПП РФ №1131, п. 12", "Приказ Минстроя №XXX"],
                "contexts": ["регистрация через госуслуги", "привязка лицевого счёта", "просмотр задолженности", "передача показаний", "жалобы и обращения", "история платежей"]
            },
            "личный кабинет ук": {
                "synonyms": ["кабинет управляющей компании", "вход для УК", "панель управления УК", "интерфейс для поставщиков услуг"],
                "norm_refs": ["ПП РФ №1131, п. 15", "Приказ Минстроя №XXX"],
                "contexts": ["регистрация организации", "загрузка отчётов", "публикация тарифов", "внесение показаний ОДПУ", "ответы на жалобы", "взаимодействие с ГИС ЖКХ"]
            },
            "подача жалобы": {
                "synonyms": ["электронная жалоба", "онлайн-жалоба", "жалоба через госуслуги", "жалоба в гис жкх", "обращение в жилинспекцию"],
                "norm_refs": ["ФЗ №59-ФЗ", "ПП РФ №1131, п. 20", "ПП РФ №1191"],
                "contexts": ["сроки рассмотрения (до 30 дней)", "обязательные поля", "прикрепление документов", "статус рассмотрения", "жалоба на УК/РСО", "апелляция решения"]
            },
            "передача показаний": {
                "synonyms": ["отправка показаний", "ввод показаний", "снятие показаний через интернет", "показания счётчиков онлайн", "телеметрия в гис жкх"],
                "norm_refs": ["ПП РФ №354, п. 31(1)", "ПП РФ №1131, п. 18"],
                "contexts": ["сроки передачи (23-25 число)", "ручной ввод vs автоматическая передача", "история передач", "ошибки при вводе", "начисление по нормативу при отсутствии данных"]
            },
            "проверка задолженности": {
                "synonyms": ["узнать долг", "проверить оплату", "история платежей", "выписка по лицевому счёту", "долг за жку"],
                "norm_refs": ["ФЗ №210-ФЗ, ст. 7", "ПП РФ №1131, п. 12(3)"],
                "contexts": ["по адресу", "по лицевому счёту", "экспорт в PDF", "оплата онлайн", "рассрочка", "ошибки в начислениях"]
            },
            "отчеты ук": {
                "synonyms": ["отчёты управляющей компании", "публичные отчёты", "финансовая отчётность ук", "план-график работ", "отчёт о выполнении работ"],
                "norm_refs": ["ЖК РФ, ст. 162", "ПП РФ №1131, п. 15(4)"],
                "contexts": ["где найти в ГИС ЖКХ", "периодичность публикации", "обязательные разделы", "жалоба на отсутствие отчётов", "сравнение с предыдущими периодами"]
            },
            "тарифы жкх": {
                "synonyms": ["тарифы на коммунальные услуги", "цены на жку", "нормативы потребления", "расчёт платы", "тарифы по регионам"],
                "norm_refs": ["ЖК РФ, ст. 157", "ПП РФ №354, раздел 3", "ПП РФ №1131, п. 15(2)"],
                "contexts": ["где посмотреть актуальные тарифы", "изменения с 1 июля", "тарифы по видам услуг", "расчёт с учётом льгот", "обжалование тарифов"]
            },
            "состав общего имущества": {
                "synonyms": ["ои многоквартирного дома", "перечень общего имущества", "что входит в ои", "реестр общего имущества", "описание общего имущества"],
                "norm_refs": ["ЖК РФ, ст. 36", "ПП РФ №491", "ПП РФ №1131, п. 15(5)"],
                "contexts": ["где посмотреть в ГИС ЖКХ", "обязанность УК по актуализации", "жалоба на отсутствие информации", "судебные споры по составу ОИ", "техническая документация"]
            },
            "электронная подпись": {
                "synonyms": ["эцп", "усиленная квалифицированная подпись", "электронная подпись для госуслуг", "подпись для юрлиц", "подпись для физлиц"],
                "norm_refs": ["ФЗ №63-ФЗ", "ПП РФ №1191, п. 8"],
                "contexts": ["как получить", "где использовать", "срок действия", "стоимость", "отказ в приёме подписи", "альтернативы (код подтверждения)"]
            },
            "мобильное приложение": {
                "synonyms": ["приложение госуслуги", "госуслуги мобильное", "app gosuslugi", "гис жкх приложение", "мобильный доступ к жкх"],
                "norm_refs": ["Приказ Минцифры №XXX", "Приказ Минстроя №XXX"],
                "contexts": ["скачать в AppStore/GooglePlay", "функционал приложения", "авторизация", "push-уведомления", "ошибки входа", "отсутствие функций в приложении"]
            },
            "техническая поддержка": {
                "synonyms": ["помощь на госуслугах", "поддержка гис жкх", "горячая линия", "чат-бот", "обратная связь", "ошибка на портале"],
                "norm_refs": ["ФЗ №210-ФЗ, ст. 10", "ПП РФ №1191, п. 12"],
                "contexts": ["контакты поддержки", "форма обратной связи", "время ответа", "жалоба на бездействие", "восстановление доступа", "ошибки загрузки страниц"]
            },
            "электронный документооборот": {
                "synonyms": ["эдо", "электронные документы", "обмен документами", "официальные документы онлайн", "цифровые акты"],
                "norm_refs": ["ФЗ №63-ФЗ", "ФЗ №149-ФЗ", "ПП РФ №1131, п. 17"],
                "contexts": ["юридическая сила", "архивирование", "подписание документов", "интеграция с УК/РСО", "жалоба на непризнание ЭДО", "технические требования"]
            },
            "удалённое обслуживание": {
                "synonyms": ["дистанционное обслуживание", "онлайн-сервисы", "цифровые сервисы жкх", "электронные услуги", "без посещения офиса"],
                "norm_refs": ["ФЗ №210-ФЗ", "ПП РФ №1191", "Национальная программа «Цифровая экономика»"],
                "contexts": ["перечень доступных услуг", "требования к оборудованию", "безопасность данных", "ограничения для пожилых", "обучение пользователей", "доступность для МГН"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "gosuslugi.ru", "dom.gosuslugi.ru", "minstroyrf.ru", "minцифры.рф", "government.ru",
            "gji.ru", "rosreestr.gov.ru", "fgis-tarif.ru", "consultant.ru", "garant.ru",
            "pravo.gov.ru", "gkh.ru", "roscomnadzor.ru", "vsrf.ru", "fias.nalog.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".pravo.gov.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ГИС ЖКХ официальный портал")
        queries.append(f"{query} госуслуги инструкция как сделать")
        queries.append(f"{query} ФЗ 210-ФЗ электронные услуги")
        queries.append(f"{query} ПП РФ 1131 ГИС ЖКХ порядок")
        queries.append(f"{query} судебная практика по жалобам через госуслуги")
        
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Госуслуги и ГИС ЖКХ
        Формирует промт:
        - Фокус: цифровые сервисы ЖКХ — портал Госуслуг, ГИС ЖКХ, регистрация, документы, ошибки
        - Строгая структура, ссылки на нормативные акты, судебная практика
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        # Проверка на запрос о пени
        penalty_keywords = ["пени", "неустойка", "штраф за просрочку", "ставка цб", "9.5%", "ключевая ставка"]
        should_calculate_penalty = any(kw in summary.lower() for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — экспертный ИИ-ассистент по цифровым сервисам ЖКХ: портал Госуслуг и ГИС ЖКХ.\n"
            "Отвечай строго по официальным источникам и предоставленному контексту.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. НИКАКИХ ГАЛЛЮЦИНАЦИЙ: если информации нет — ответь: 'Недостаточно данных для точного ответа.'\n"
            "2. ОБЯЗАТЕЛЬНО указывай ссылки на нормативные акты.\n"
            "3. СТРУКТУРА: краткий вывод → нормативное обоснование → пошаговая инструкция → судебная практика.\n"
            "4. ФОРМУЛЫ ТОЛЬКО ПРИ ЗАПРОСЕ о пени.\n"
            "5. Приоритет: ФЗ №210-ФЗ > ПП РФ №1131 > ФЗ №63-ФЗ > ПП РФ №354 > судебная практика.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "--- Основной ответ ---\n"
            "Краткий вывод: [что делать, где найти, законно ли требование, время выполнения]\n"
            "Нормативное обоснование: [ФЗ №210-ФЗ, ПП РФ №1131, ФЗ №63-ФЗ, ПП РФ №354]\n"
            "Пошаговая инструкция:\n"
            "- Регистрация на портале Госуслуг / ГИС ЖКХ (ФЗ №210-ФЗ, ст.6)\n"
            "- Поиск нужного сервиса и раздела\n"
            "- Подготовка документов и данных (паспорт, СНИЛС, лицевой счёт, ЭЦП)\n"
            "- Пошаговое использование интерфейса\n"
            "- Действия при ошибках или отказах (техподдержка, жалоба, ГЖИ)\n"
            "Судебная практика:\n"
            "- Определение ВС РФ №XXX-ЭСXX-XXXX — краткая позиция суда\n"
            "Если судебных решений нет: 'Судебная практика по данному вопросу в базе отсутствует'.\n"
            "Ключевые нормативные акты:\n"
            "- ФЗ №210-ФЗ «Об организации предоставления государственных и муниципальных услуг»\n"
            "- ПП РФ №1131 «Об утверждении Правил функционирования ГИС ЖКХ»\n"
            "- ФЗ №63-ФЗ «Об электронной подписи»\n"
            "- ПП РФ №354 (если вопрос касается ЖКУ)\n"
            "- ФЗ №59-ФЗ «О порядке рассмотрения обращений граждан»\n"
        )
    
        # --- Блок расчёта пени ---
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчёт пени (если упомянут):**\n"
                "- Формула: Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Основание: [ЖК РФ, ст.155.1], [ФЗ №44-ФЗ], [ПП РФ №329]\n"
                "- Ограничение: не более 9.5% годовых до 2027 года\n"
                "- Пример: 10 000 руб., просрочка 30 дней → 95 руб.\n"
                "- Начало начисления: с 31-го дня после окончания срока оплаты\n"
            )
    
        system_prompt += f"{self.get_role_instruction(role)}"
    
        # --- Формат для LLaMA / QVikhr ---
        prompt_formatted = f"{system_prompt}"
    
        return prompt_formatted

class OwnerMeetingAgent(RAGAgent):
    def __init__(self):
        # Строим семантическую карту терминов
        self.term_map = self._build_term_map()
        keywords = self._flatten_term_map(self.term_map)
        
        super().__init__("Собственники и Собрания", keywords)

    def _build_term_map(self) -> Dict[str, Any]:
        """Строит расширенную семантическую карту терминов с синонимами, контекстами и нормативными ссылками."""
        return {
            "собственник помещения": {
                "synonyms": ["владелец квартиры", "собственник жилья", "участник осс", "член осс", "жилец-собственник"],
                "norm_refs": ["ЖК РФ, ст. 30", "ГК РФ, ст. 209", "ФЗ №189-ФЗ, ст. 36"],
                "contexts": ["права на участие в осс", "обязанность оплачивать жку", "ответственность за содержание", "право на информацию", "право на оспаривание решений"]
            },
            "общее собрание собственников": {
                "synonyms": ["осс", "собрание жильцов", "собрание собственников", "внеочередное собрание", "очное собрание", "заочное собрание"],
                "norm_refs": ["ЖК РФ, ст. 44-48", "ПП РФ №416", "ПП РФ №1131 (для ГИС ЖКХ)"],
                "contexts": ["инициатор собрания", "сроки созыва", "форма проведения", "повестка дня", "уведомление собственников", "кворум", "голосование", "протокол"]
            },
            "протокол собрания": {
                "synonyms": ["протокол осс", "итоги голосования", "акт собрания", "официальный документ осс", "результаты голосования"],
                "norm_refs": ["ЖК РФ, ст. 46(4)", "ПП РФ №416, п. 15", "ПП РФ №1131, п. 19"],
                "contexts": ["обязательные реквизиты", "сроки подписания", "публикация в ГИС ЖКХ", "ошибки в протоколе", "оспаривание протокола", "хранение в течение 3 лет"]
            },
            "кворум": {
                "synonyms": ["кворум осс", "требуемое количество голосов", "порог голосования", "минимальное участие", "доля голосов для решения"],
                "norm_refs": ["ЖК РФ, ст. 46(1)", "ПП РФ №416, п. 10"],
                "contexts": ["50%+1 голос — большинство вопросов", "2/3 голосов — реконструкция, переустройство", "расчёт по площади", "учёт голосов отсутствующих", "повторное собрание с меньшим кворумом"]
            },
            "голосование": {
                "synonyms": ["способ голосования", "форма голосования", "очное голосование", "заочное голосование", "электронное голосование", "голосование через ГИС ЖКХ"],
                "norm_refs": ["ЖК РФ, ст. 47", "ПП РФ №416, п. 8", "ПП РФ №1131, п. 19(3)"],
                "contexts": ["письменные бюллетени", "голосование в ГИС ЖКХ", "сроки приёма голосов", "подтверждение личности", "анонимность голосования", "жалоба на подтасовку"]
            },
            "инициатор собрания": {
                "synonyms": ["кто может созвать собрание", "инициатор осс", "организатор собрания", "по инициативе собственников", "по инициативе ук"],
                "norm_refs": ["ЖК РФ, ст. 45(2)", "ПП РФ №416, п. 3"],
                "contexts": ["любой собственник", "совет дома", "УК/ТСЖ", "уведомление за 10 дней", "форма уведомления", "обязанность УК созывать по требованию"]
            },
            "повестка дня": {
                "synonyms": ["вопросы собрания", "программа собрания", "перечень вопросов", "что можно включить в повестку", "изменение повестки"],
                "norm_refs": ["ЖК РФ, ст. 45(3)", "ПП РФ №416, п. 5"],
                "contexts": ["обязательные вопросы", "вопросы по инициативе собственников", "ограничения по содержанию", "изменение повестки до собрания", "жалоба на незаконную повестку"]
            },
            "оспаривание решения": {
                "synonyms": ["отмена решения осс", "жалоба на решение собрания", "судебное оспаривание", "недействительное решение", "признание решения недействительным"],
                "norm_refs": ["ЖК РФ, ст. 46(6)", "ГПК РФ, ст. 131", "КАС РФ, ст. 218"],
                "contexts": ["срок 6 месяцев", "основания: нарушение процедуры, кворума, повестки", "доказательства: протокол, уведомления", "моральный вред", "госпошлина", "позиция ВС РФ"]
            },
            "совет многоквартирного дома": {
                "synonyms": ["совет дома", "совет мкд", "орган управления дома", "представитель собственников", "комитет дома"],
                "norm_refs": ["ЖК РФ, ст. 161.1", "ПП РФ №416, п. 20"],
                "contexts": ["избрание на осс", "полномочия: контроль УК, подготовка осс, приёмка работ", "срок полномочий — 2 года", "отчёт перед собственниками", "досрочное прекращение полномочий"]
            },
            "председатель совета дома": {
                "synonyms": ["председатель совета", "глава совета дома", "координатор собственников", "представитель совета"],
                "norm_refs": ["ЖК РФ, ст. 161.1(4)", "ПП РФ №416, п. 21"],
                "contexts": ["избирается из членов совета", "подписание документов", "взаимодействие с УК", "право подписи", "ответственность за действия", "отзыв председателя"]
            },
            "электронное голосование": {
                "synonyms": ["голосование через госуслуги", "голосование в гис жкх", "онлайн-голосование", "дистанционное голосование", "электронный бюллетень"],
                "norm_refs": ["ПП РФ №1131, п. 19(3)", "ФЗ №210-ФЗ", "ПП РФ №416, п. 8(5)"],
                "contexts": ["требуется аккаунт на госуслугах", "сроки проведения", "подтверждение голоса ЭЦП", "результаты в реальном времени", "жалоба на сбои", "юридическая сила электронного голоса"]
            },
            "повторное собрание": {
                "synonyms": ["второе собрание", "резервное собрание", "собрание при отсутствии кворума", "пересобрание"],
                "norm_refs": ["ЖК РФ, ст. 46(1)", "ПП РФ №416, п. 10(3)"],
                "contexts": ["созывается, если не набран кворум", "срок — не позднее 30 дней", "кворум — не менее 30%", "те же вопросы повестки", "решение принимается при любом кворуме"]
            },
            "решение осс": {
                "synonyms": ["итоги голосования", "утверждённое решение", "решение собрания", "вступление решения в силу", "исполнение решения"],
                "norm_refs": ["ЖК РФ, ст. 46(5)", "ПП РФ №416, п. 16"],
                "contexts": ["вступает в силу через 10 дней", "обязательно для всех собственников", "обязательно для УК/ТСЖ", "жалоба на неисполнение", "исковые требования", "исполнение через суд"]
            },
            "обязанности собственника": {
                "synonyms": ["что должен собственник", "обязанности по жку", "оплата коммунальных услуг", "участие в осс", "содержание имущества"],
                "norm_refs": ["ЖК РФ, ст. 30, 39, 153", "ГК РФ, ст. 210"],
                "contexts": ["оплата жку и капремонта", "участие в осс", "сохранность общего имущества", "допуск к инженерным системам", "ответственность за ущерб", "штрафы за нарушения"]
            },
            "судебная практика по осс": {
                "synonyms": ["оспаривание решений осс", "признание недействительным", "нарушение кворума", "незаконная повестка", "позиция вс рф по осс"],
                "norm_refs": ["КАС РФ, ст. 218", "Определения ВС РФ", "Обзоры судебной практики ВС РФ"],
                "contexts": ["основания для отмены", "доказательства нарушений", "срок исковой давности", "расходы на экспертизу", "моральный вред", "госпошлина и её возврат"]
            },
        }

    def _flatten_term_map(self, term_map: Dict) -> List[str]:
        """Преобразует структурированный словарь в плоский список уникальных ключевых слов."""
        keywords = set()
        for term, data in term_map.items():
            keywords.add(term.lower())  # оригинальный ключ
            for synonym in data.get("synonyms", []):
                keywords.add(synonym.lower())
            # Добавляем ключи из контекстов
            contexts = data.get("contexts", [])
            if isinstance(contexts, dict):
                for ctx_key in contexts.keys():
                    keywords.add(ctx_key.lower())
            elif isinstance(contexts, list):
                for ctx in contexts:
                    keywords.add(ctx.lower())
        return list(keywords)

    def _perform_web_search(self, query: str, max_results: int = 3) -> str:
        """
        Улучшенный веб-поиск: приоритет официальным источникам, фильтрация, ранжирование.
        Выполняется ВНУТРИ _build_prompt, как в оригинальной архитектуре.
        """
        OFFICIAL_DOMAINS = {
            "consultant.ru", "garant.ru", "pravo.gov.ru", "gji.ru", "minstroyrf.ru",
            "vsrf.ru", "sudrf.ru", "правосудие.рф", "rosreestr.gov.ru", "gkh.ru",
            "government.ru", "kremlin.ru", "fgis-tarif.ru"
        }

        BLACKLISTED_DOMAINS = {
            "otvet.mail.ru", "ask.fm", "irecommend.ru", "pikabu.ru",
            "zen.yandex.ru", "thequestion.ru", "quora.com", "reddit.com",
            "fishki.net", "yaplakal.com", "blog", "forum"
        }

        # Генерируем расширенные поисковые запросы
        expanded_queries = self._expand_search_query(query)
        all_results = []

        for attempt in range(2):
            try:
                with DDGS(timeout=10) as ddgs:
                    for q in expanded_queries:
                        results = ddgs.text(q, max_results=5)
                        for r in results:
                            href = r.get('href', '')
                            if not href:
                                continue
                            
                            try:
                                domain = href.split('/')[2].lower()
                            except IndexError:
                                continue

                            if any(bad in domain for bad in BLACKLISTED_DOMAINS):
                                continue

                            weight = 3 if any(official in domain for official in OFFICIAL_DOMAINS) else \
                                     2 if any(gov in domain for gov in [".gov.ru", ".gkh.ru", ".sudrf.ru", ".vsrf.ru"]) else 1

                            snippet = {
                                "body": r['body'],
                                "href": href,
                                "title": r.get('title', ''),
                                "weight": weight
                            }
                            all_results.append(snippet)

                    all_results = sorted(all_results, key=lambda x: x['weight'], reverse=True)
                    seen_bodies = set()
                    unique_results = []
                    for r in all_results:
                        body_hash = hash(r['body'][:100])
                        if body_hash not in seen_bodies:
                            seen_bodies.add(body_hash)
                            unique_results.append(r)
                            if len(unique_results) >= max_results:
                                break

                    if unique_results:
                        formatted = []
                        for r in unique_results:
                            prefix = "[ОФИЦИАЛЬНЫЙ ИСТОЧНИК] " if r['weight'] >= 2 else ""
                            formatted.append(f"{prefix}• {r['body']}\n  Источник: {r['href']}\n")
                        return "\n".join(formatted).strip()
                    else:
                        return "По вашему запросу ничего не найдено в надёжных источниках."

            except Exception as e:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return f"Ошибка веб-поиска: {str(e)}"

        return "Не удалось выполнить веб-поиск. Попробуйте позже."

    def _expand_search_query(self, query: str) -> List[str]:
        """Генерирует несколько вариантов поискового запроса для лучшего покрытия темы."""
        queries = [query]
        queries.append(f"{query} ЖК РФ ст 44-48 общее собрание собственников")
        queries.append(f"{query} ПП РФ 416 порядок проведения осс")
        queries.append(f"{query} судебная практика оспаривание решения осс")
        queries.append(f"{query} кворум осс 50% или 2/3 ЖК РФ")
        queries.append(f"{query} протокол общего собрания образец ПП РФ 416")
        queries.append(f"{query} совет дома полномочия ЖК РФ ст 161.1")
        
        # Добавляем синонимы
        for term, data in self.term_map.items():
            if term in query.lower() or any(syn in query.lower() for syn in data.get("synonyms", [])):
                for synonym in data.get("synonyms", [])[:2]:
                    new_q = query.replace(term, synonym) if term in query else query + " " + synonym
                    queries.append(new_q)
        return list(set(queries))

    def _build_prompt(self, summary: str, context_text: str, role: str = "смешанная") -> str:
        """
        Агент: Собственники и Собрания
        Формирует промт:
        - Фокус: права собственников, ОСС — инициаторы, повестка, кворум, протокол, оспаривание
        - Строгая структура, ссылки на нормативные акты, судебная практика
        """
        extra = self.improve_prompt_from_feedback()
        web_results = self._perform_web_search(summary)
    
        # Проверка на запрос о пени
        penalty_keywords = ["пени", "неустойка", "штраф за просрочку", "ставка цб", "9.5%", "ключевая ставка"]
        should_calculate_penalty = any(kw in summary.lower() for kw in penalty_keywords)
    
        # --- SYSTEM PROMPT ---
        system_prompt = (
            "Ты — экспертный ИИ-ассистент по правам собственников и организации общих собраний в МКД.\n"
            "Отвечай строго по официальным источникам и предоставленному контексту.\n\n"
            "**ЖЕСТКИЕ ПРАВИЛА:**\n"
            "1. НИКАКИХ ГАЛЛЮЦИНАЦИЙ: если информации нет — ответь: 'Недостаточно данных для точного ответа.'\n"
            "2. ОБЯЗАТЕЛЬНО указывай ссылки на нормативные акты.\n"
            "3. СТРУКТУРА: краткий вывод → нормативное обоснование → пошаговая инструкция → судебная практика.\n"
            "4. ФОРМУЛЫ ТОЛЬКО ПРИ ЗАПРОСЕ о пени.\n"
            "5. Приоритет: ЖК РФ > ПП РФ №416 > судебная практика > ПП РФ №1131.\n\n"
            f"### Контекст:\n{context_text}\n\n"
            f"### Веб-поиск:\n{web_results}\n\n"
            f"### Дополнительные обновления:\n{extra}\n\n"
            "--- Основной ответ ---\n"
            "Краткий вывод: [что делать, кто отвечает, законно ли требование]\n"
            "Нормативное обоснование: [ЖК РФ, ст.44-48, 161.1; ПП РФ №416; ПП РФ №1131]\n"
            "Пошаговая инструкция:\n"
            "- Кто может быть инициатором собрания? (ЖК РФ, ст.45)\n"
            "- Составление повестки и уведомление собственников (ПП РФ №416, п.5-6)\n"
            "- Расчёт кворума и проведение голосования (ЖК РФ, ст.46-47)\n"
            "- Оформление и публикация протокола (ПП РФ №416, п.15; ПП РФ №1131, п.19)\n"
            "- Оспаривание решения собрания (ЖК РФ, ст.46(6); срок 6 месяцев)\n"
            "- Полномочия совета дома и председателя (ЖК РФ, ст.161.1)\n"
            "Судебная практика:\n"
            "- Определение ВС РФ №XXX-ЭСXX-XXXX — краткая позиция суда\n"
            "Если судебных решений нет: 'Судебная практика по данному вопросу в базе отсутствует'.\n"
            "Ключевые нормативные акты:\n"
            "- ЖК РФ (Глава 6, ст.44-48; ст.161.1)\n"
            "- ПП РФ №416 «Об утверждении Правил проведения общих собраний…»\n"
            "- ПП РФ №1131 (ГИС ЖКХ и электронное голосование)\n"
            "- ГПК РФ / КАС РФ (оспаривание решений)\n"
        )
    
        # --- Блок расчёта пени ---
        if should_calculate_penalty:
            system_prompt += (
                "\n**Расчёт пени (если упомянут):**\n"
                "- Формула: Пени = Сумма долга × Дни просрочки × (Ключевая ставка ЦБ РФ / 300 / 100)\n"
                "- Основание: [ЖК РФ, ст.155.1], [ФЗ №44-ФЗ], [ПП РФ №329]\n"
                "- Ограничение: не более 9.5% годовых до 2027 года\n"
                "- Пример: 10 000 руб., просрочка 30 дней → 95 руб.\n"
                "- Начало начисления: с 31-го дня после окончания срока оплаты\n"
            )
    
        system_prompt += f"{self.get_role_instruction(role)}"
    
        # --- Формат для LLaMA / QVikhr ---
        prompt_formatted = f"{system_prompt}"
    
        return prompt_formatted

# ---------------------------
# MetaAgent — координатор мультиагентного диалога
# ---------------------------
class MetaAgent:
    def __init__(self, rag_system):
        self.rag_system = rag_system
        self.agents = rag_system.agents
        self.dialog_log = []

    def route_intelligently(self, query: str) -> Tuple[Optional[RAGAgent], List[RAGAgent]]:
        """
        Интеллектуальная маршрутизация запроса.
        Возвращает кортеж: (основной агент, список вспомогательных агентов для консультации)
        """
        # Находим всех кандидатов, чьи ключевые слова совпадают с запросом
        primary_candidates = [a for a in self.agents if a.matches(query) and not isinstance(a, FallbackAgent)]
        secondary_candidates = []

        # Если нет кандидатов, возвращаем Fallback
        if not primary_candidates:
            fallback = next((a for a in self.agents if isinstance(a, FallbackAgent)), None)
            return fallback, []

        # Выбираем основного агента по количеству совпадений ключевых слов
        def match_score(agent: RAGAgent, qry: str) -> int:
            q_words = set(re.findall(r'\b[а-яёa-z0-9]+\b', qry.lower()))
            return sum(1 for kw in agent.keywords if kw in q_words)

        primary_agent = max(primary_candidates, key=lambda a: match_score(a, query))

        # Определяем вспомогательных агентов на основе типа основного агента
        # Это эвристика, основанная на типичных комбинациях вопросов из FAQ

        if isinstance(primary_agent, EmergencyAgent):
            # При аварии часто нужны данные по подрядчикам и контролю качества (для перерасчёта)
            secondary_candidates = [
                a for a in self.agents 
                if isinstance(a, (ContractorAgent, QualityControlAgent, WasteManagementAgent))
            ]

        elif isinstance(primary_agent, TariffAgent):
            # При вопросах по начислениям часто нужны данные по приборам учёта и аудиту
            secondary_candidates = [
                a for a in self.agents 
                if isinstance(a, (MeterAgent, BillingAuditAgent, PaymentDocumentsAgent))
            ]

        elif isinstance(primary_agent, WasteManagementAgent):
            # Вывоз ТКО часто связан с нормативами и раскрытием информации
            secondary_candidates = [
                a for a in self.agents 
                if isinstance(a, (NormativeAgent, DisclosureAgent, ContractorAgent))
            ]

        elif isinstance(primary_agent, TechnicalAgent):
            # Технические вопросы могут требовать нормативной базы или данных по капремонту
            secondary_candidates = [
                a for a in self.agents 
                if isinstance(a, (NormativeAgent, CapitalRepairAgent, QualityControlAgent))
            ]

        elif isinstance(primary_agent, LegalClaimsAgent):
            # Юридические претензии требуют знания нормативов и практики взыскания
            secondary_candidates = [
                a for a in self.agents 
                if isinstance(a, (NormativeAgent, DebtManagementAgent, TariffAgent))
            ]

        # 🆕 ДОБАВЛЕНО: Если основной агент — ContractorAgent, подключаем HistoryAgent
        elif isinstance(primary_agent, ContractorAgent):
            secondary_candidates = [
                a for a in self.agents 
                if isinstance(a, (HistoryAgent, QualityControlAgent))
            ]

        # Убираем основного агента из списка вспомогательных (если вдруг попал)
        secondary_candidates = [a for a in secondary_candidates if a != primary_agent]

        return primary_agent, secondary_candidates

    def route(self, query: str, exclude_agent: RAGAgent = None) -> Optional[RAGAgent]:
        """
        Устаревший метод для обратной совместимости.
        Просто возвращает основного агента.
        """
        primary, _ = self.route_intelligently(query)
        return primary if primary != exclude_agent else None

    def should_consult_others(self, agent: RAGAgent, query: str) -> bool:
        """
        Устаревший метод. Теперь логика координации полностью в route_intelliginely.
        Возвращает True, если есть хотя бы один вспомогательный агент.
        """
        _, secondary = self.route_intelligently(query)
        return len(secondary) > 0

    def log_dialog(self, main_agent: str, consulted_agents: List[str], final_answer: str, query: str):
        """
        Логирует результат мультиагентного диалога.
        """
        entry = {
            "timestamp": time.time(),
            "query": query,
            "main_agent": main_agent,
            "consulted_agents": consulted_agents,
            "final_answer": final_answer[:500]  # Ограничиваем длину для лога
        }
        self.dialog_log.append(entry)
        # Сохраняем лог в файл
        try:
            with open("multi_agent_log.json", "w", encoding="utf-8") as f:
                json.dump(self.dialog_log, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Ошибка при сохранении лога диалога: {e}")
            
# ---------------------------
# Основной RAGSystem с поддержкой агентов
# ---------------------------

class RAGSystem:
    def __init__(self):
        self.embedding_model = embedding_model
        self.tokenizer = tokenizer
        self.model = model
        self.index = index
        self.chunks_data = chunks_data
        self.model_ctx_tokens = 8000
        self.max_context_tokens = int(self.model_ctx_tokens * 0.8)
        self.chunk_embeddings = None
        self.enable_clarification = False

        self.agents = [
            TariffAgent(),
            NormativeAgent(),
            TechnicalAgent(),
            MeterAgent(),
            DebtAgent(),
            DisclosureAgent(),
            IoTAgent(),
            MeetingAgent(),
            CapitalRepairAgent(),
            EmergencyAgent(),
            ContractorAgent(),
            HistoryAgent(),
            QualityControlAgent(),
            PaymentDocumentsAgent(),
            AccountManagementAgent(),
            BillingAuditAgent(),
            SubsidyAndBenefitsAgent(),
            LegalClaimsAgent(),
            ContractAndMeetingAgent(),
            DebtManagementAgent(),
            IoTIntegrationAgent(),
            WasteManagementAgent(),
            RegionalMunicipalAgent(),
            CourtPracticeAgent(),
            LicensingControlAgent(),
            RSOInteractionAgent(),
            SafetySecurityAgent(),
            EnergyEfficiencyAgent(),
            ReceiptProcessingAgent(),
            PassportRegistrationAgent(),
            RecalculationAgent(),
            CommonPropertyAgent(),
            DisputeResolutionAgent(),
            ProceduralAgent(),
            NPBAgent(),
            IPUODPUAgent(),
            GISGKHAgent(),
            OwnerMeetingAgent(),
            FallbackAgent()
        ]

        self.meta_agent = MetaAgent(self)

    def detect_user_role(self, query: str) -> str:
        """
        Улучшенное определение роли пользователя на основе контекста и ключевых фраз.
        """
        text = query.lower()
    
        # Приоритет 1: Явные фразы-маркеры
        if any(phrase in text for phrase in [
            "я собственник", "я житель", "моя квартира", "мой дом", "мне начислили", "я хочу узнать",
            "как мне", "для меня", "мой лицевой счет", "мои показания", "я проживаю"
        ]):
            return "житель"
    
        if any(phrase in text for phrase in [
            "мы как ук", "мы тсн", "наша компания", "начисляем", "передаем рсо", "акт сверки с рсо",
            "расчет с рсо", "должны заплатить рсо", "исполнитель", "начислятор", "наш дом", "наши жильцы"
        ]):
            return "исполнитель"
    
        # Приоритет 2: Контекстный анализ (если явных маркеров нет)
        # Считаем баллы
        resident_score = sum([
            2 if "мой" in text or "мне" in text else 0,
            1 if "пересчитайте" in text else 0,
            1 if "почему так много" in text else 0,
            1 if "как оплатить" in text else 0,
            1 if "вызовите мастера" in text else 0,
        ])
    
        executor_score = sum([
            2 if "мы" in text and ("ук" in text or "тсн" in text or "компания" in text) else 0,
            1 if "начисляем" in text else 0,
            1 if "рсо" in text and ("передаем" in text or "платим" in text) else 0,
            1 if "акт сверки" in text else 0,
            1 if "расчет" in text and "жильцам" in text else 0,
        ])
    
        if resident_score > executor_score:
            return "житель"
        elif executor_score > resident_score:
            return "исполнитель"
        else:
            return "смешанная"

    def _encode_texts(self, texts: List[str], prompt_name: str) -> np.ndarray:
        emb = self.embedding_model.encode(texts, prompt_name=prompt_name, convert_to_numpy=True, normalize_embeddings=True)
        return emb.astype('float32')

    def _preprocess_query(self, query: str) -> str:
        tokens = re.findall(r"\w+|\S", query.lower())
        filtered = [t for t in tokens if t.isalpha() and t not in STOP_WORDS or not t.isalpha()]
        cleaned = " ".join(filtered).strip()
        return cleaned if cleaned else query

    def precompute_chunk_embeddings(self, batch_size: int = 256):
        if not self.chunks_data:
            return
        texts = [c['content'] for c in self.chunks_data]
        embs = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i+batch_size]
            embs.append(self._encode_texts(batch, prompt_name="search_document"))
        self.chunk_embeddings = np.vstack(embs)

    def analyze_query_for_clarification(self, original_query: str) -> Tuple[bool, Optional[str], Optional[str]]:
        if not self.enable_clarification:
            return False, None, original_query
        if not original_query.strip():
            return False, None, original_query

        analysis_prompt = (
            f"Пользователь: {original_query}\n\n"
            f"Инструкция:\n"
            f"Ты — юрист по вопросам ЖКХ. Анализируй запрос и формируй резюме или уточняющий вопрос строго на основании документов.\n"
            f"Если запрос ясен — формулируй summary. Если неясен — задавай уточняющий вопрос.\n"
            f"Ассистент:[SEP]"
        )

        try:
            inputs = self.tokenizer(analysis_prompt, return_tensors="pt", truncation=True, max_length=512).to(device)
            with torch.no_grad():
                outputs = self.model.generate(**inputs, max_new_tokens=150, temperature=0.1, do_sample=False, pad_token_id=self.tokenizer.eos_token_id)
            raw_analysis = self.tokenizer.decode(outputs[0], skip_special_tokens=False)
            start_marker = "Ассистент:[SEP]"
            start = raw_analysis.find(start_marker)
            if start != -1:
                analysis_part = raw_analysis[start + len(start_marker):].strip()
            else:
                analysis_part = raw_analysis.strip()
            for stop in ["</s>", "Пользователь:", "\n\n"]:
                pos = analysis_part.find(stop)
                if pos != -1:
                    analysis_part = analysis_part[:pos].strip()

            if analysis_part.startswith("Уточните, пожалуйста,"):
                question = analysis_part[len("Уточните, пожалуйста,"):].strip().rstrip("?") + "?"
                return True, question, None
            elif "Вопрос об:" in analysis_part or "Запрос о" in analysis_part:
                summary = analysis_part.replace("Вопрос об:", "").replace("Запрос о", "").strip()
                return False, None, summary
            else:
                return True, "Уточните, пожалуйста, суть вашей проблемы по ЖКХ.", None

        except Exception as e:
            print(f"❌ Ошибка анализа вопроса: {e}")
            return True, "Не удалось понять запрос. Пожалуйста, переформулируйте его.", None

    def search_relevant_chunks(self, query: str, role: str = "смешанная", top_k: int = 5) -> List[Dict]:
        if self.index is None or not self.chunks_data:
            return []
    
        query_vector = self._encode_texts([query], prompt_name="search_query")
        scores, indices = self.index.search(query_vector, top_k * 3)  # Берем больше кандидатов для фильтрации
        results = []
        for idx, score in zip(indices[0], scores[0]):
            if idx == -1: continue
            chunk = self.chunks_data[idx].copy()
            chunk["score"] = float(score)
            results.append(chunk)
    
        # НОВЫЙ БЛОК: Контекстный бустинг по тегам и ключевым словам
        q_lower = query.lower()
    
        # Определяем основную тему запроса
        theme_boost = {
            "авария": 1.5 if any(kw in q_lower for kw in ["авария", "прорыв", "затопило", "отключили", "срочно"]) else 1.0,
            "тариф": 1.5 if any(kw in q_lower for kw in ["тариф", "начисление", "плата", "стоимость", "перерасчет"]) else 1.0,
            "счетчик": 1.5 if any(kw in q_lower for kw in ["счетчик", "ипу", "одпу", "поверка", "показания"]) else 1.0,
            "тко": 1.5 if any(kw in q_lower for kw in ["тко", "мусор", "вывоз", "контейнер", "мусорная площадка"]) else 1.0,
            "собрание": 1.5 if any(kw in q_lower for kw in ["собрание", "осс", "голосование", "протокол"]) else 1.0,
        }
    
        for r in results:
            tags = [t.lower() for t in r.get("tags", [])]
            content = r.get('content', '').lower()
    
            # Бустинг по тегам
            for theme, boost in theme_boost.items():
                if theme in tags or theme in content:
                    r["score"] *= boost
    
            # Бустинг по роли (оставляем, но делаем менее агрессивным)
            if role == "житель":
                if "пп рф" in content or "фз" in content: r["score"] *= 1.1
                if "вс рф" in content: r["score"] *= 0.95
            elif role == "исполнитель":
                if "вс рф" in content or "арбитраж" in content: r["score"] *= 1.2
                if "пп рф" in content: r["score"] *= 1.1
    
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]

    def _truncate_context_by_tokens(self, chunks_with_scores: List[Tuple[dict, float]], max_tokens_est: int):
        chunks_with_scores.sort(key=lambda x: -x[1])
        out, total = [], 0
        for chunk, score in chunks_with_scores:
            content = chunk.get('content', '').strip()
            token_count = estimate_tokens(content)
            if total + token_count > max_tokens_est:
                if total == 0:
                    sentences = sent_tokenize(content)
                    for sent in sentences:
                        sent_tokens = estimate_tokens(sent)
                        if total + sent_tokens > max_tokens_est: break
                        out.append(({'content': sent, 'source_file': chunk.get('source_file')}, score))
                        total += sent_tokens
                break
            else:
                out.append((chunk, score))
                total += token_count
        return out

    def ensure_key_cases(self, query: str, context_chunks: List[Tuple[dict, float]]) -> List[Tuple[dict, float]]:
        themes = {
            "ГВС": ["гвс", "одпу", "подогрев", "тепловая энергия"],
            "капремонт": ["капремонт", "фонд капитального ремонта"],
            "ИПУ": ["ипу", "индивидуальный прибор учета", "счетчик", "поверка"],
            "ОДН": ["одн", "общедомовые нужды"],
            "долги": ["задолженность", "долг", "неуплата"]
        }
        q_lower = query.lower()
        matched_themes = set()
        for theme, kws in themes.items():
            if any(kw in q_lower for kw in kws):
                matched_themes.add(theme)
        if matched_themes:
            for c in self.chunks_data:
                tags = [t.lower() for t in c.get("tags", [])]
                if any(t in [m.lower() for m in matched_themes] for t in tags):
                    if c not in [x[0] for x in context_chunks]:
                        context_chunks.append((c, 0.95))
        return context_chunks

    def _sanitize_answer(self, answer: str, context_text: str) -> str:
        answer = answer.replace("[NL]", "")
    
        # Удаляем ссылки и телефоны, отсутствующие в контексте
        urls = re.findall(r'https?://\S+|www\.\S+', answer)
        for u in urls:
            if u not in context_text: answer = answer.replace(u, "[ссылка отсутствует в контексте]")
    
        phones = re.findall(r'(?:(?:\+7|8)\s?[\(\-]?\d{3}[\)\-]?\s?\d{3}[\- ]?\d{2}[\- ]?\d{2})', answer)
        for p in phones:
            if p not in context_text: answer = answer.replace(p, "[телефон отсутствует в контексте]")
    
        # НОВАЯ ПРОВЕРКА: Блокировка галлюцинаций
        hallucination_triggers = [
            "я не знаю", "не могу ответить", "этого нет в документах", "извините",
            "к сожалению", "увы", "к сожалению, я не могу", "не имею информации"
        ]
    
        if any(trigger in answer.lower() for trigger in hallucination_triggers):
            return (
                "⚠️ Похоже, в моей базе знаний пока нет точной информации по вашему запросу. "
                "Пожалуйста, переформулируйте вопрос или обратитесь в управляющую компанию напрямую. "
                "Я учусь на каждом вашем запросе!"
            )
    
        # Проверка на противоречие ключевым фактам из контекста
        # (Опционально, для продвинутых систем)
        # if "не относится к зоне ответственности ук" in context_text.lower() and "ук обязана" in answer.lower():
        #     return "⚠️ Обнаружено противоречие. Пожалуйста, задайте вопрос более точно."
    
        return answer.strip()

    def _llm_complete(self, query: str,  agent: RAGAgent, context_text: str, role: str = "смешанная", max_tokens: int = 2048, temperature: float = 0.1) -> str:
        """
        Генерация ответа с Saiga/LLaMA-3 8B.
        Формирует system prompt через _build_prompt и user prompt через query.
        """
        try:
            # --- Формируем system prompt с контентом ---
            system_prompt = agent._build_prompt(summary=query, context_text=context_text, role=role)
    
            # --- Применяем шаблон чата Saiga ---
            prompt = self.tokenizer.apply_chat_template([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query}
            ], tokenize=False, add_generation_prompt=True)
    
            # --- Токенизация ---
            data = self.tokenizer(prompt, return_tensors="pt", add_special_tokens=False)
            data = {k: v.to(self.model.device) for k, v in data.items()}
    
            # --- Генерация ---
            with torch.no_grad():
                output_ids = self.model.generate(
                    **data,
                    max_new_tokens=3000,
                    temperature=temperature,
                    top_p=0.95,
                    do_sample=True,
                    pad_token_id=self.tokenizer.eos_token_id
                )[0]
    
            # --- Обрезаем входные токены и декодируем ответ ---
            output_ids = output_ids[len(data["input_ids"][0]):]
            answer = self.tokenizer.decode(output_ids, skip_special_tokens=True).strip()
    
            # --- Замена [NL] на переносы строк ---
            answer = answer.replace("[NL]", "\n")
    
            return answer if answer else "[Ответ не сгенерирован]"
    
        except Exception as e:
            print(f"❌ Ошибка генерации: {e}")
            return "Извините, не удалось сгенерировать ответ."


    # ➕ Новый метод: генерация контекста для другого агента
    def generate_context_for_agent(self, query: str, agent: RAGAgent, role: str = "смешанная") -> str:
        """Генерирует контекст специально для другого агента (без полного ответа)"""
        if not self.chunks_data or self.index is None:
            return "Нет данных"
        chunks_with_scores = [(c, c.get('score', 1.0)) for c in self.search_relevant_chunks(query, role=role, top_k=5)]
        truncated = self._truncate_context_by_tokens(chunks_with_scores, 1000)
        context_text = "\n\n".join([c['content'].strip() for c, _ in truncated]) if truncated else "Нет данных"
        return context_text[:800]

    def generate_answer_chat(self, query: str, clarification: Optional[str] = None, max_tokens: int = 2048) -> str:
        """
        Основной метод генерации ответа.
        Использует только стандартный промпт основного агента (_build_prompt).
        """
        if self.index is None or not self.chunks_data:
            return "Нет данных в базе."
    
        # Определяем роль пользователя
        user_role = self.detect_user_role(query)
    
        # Выбираем основной агент через MetaAgent
        primary_agent, _ = self.meta_agent.route_intelligently(query)
        if not primary_agent:
            primary_agent = self.agents[0]  # fallback
    
        print(f"?? Выбран основной агент: {primary_agent.name}")
    
        if isinstance(primary_agent, FallbackAgent):
            return primary_agent.generate_fallback_response(query)
    
        # --- Шаг 1: Формируем контекст ---
        chunks_with_scores = [(c, c.get('score', 1.0)) for c in self.search_relevant_chunks(query, role=user_role, top_k=100)]
        chunks_with_scores = self.ensure_key_cases(query, chunks_with_scores)
        ctx_budget = max(1500, min(self.max_context_tokens - (max_tokens + 512), 8000))
        truncated = self._truncate_context_by_tokens(chunks_with_scores, ctx_budget)
        primary_context_text = "".join([c['content'].strip() for c, _ in truncated]) if truncated else "Нет данных в базе."
    
        # --- Шаг 2: Генерация ответа ---
        final_answer = self._llm_complete(
            query=query,
            context_text=primary_context_text,
            agent=primary_agent,
            role=user_role,
            max_tokens=max_tokens,
            temperature=0.3
        )
    
        # --- Шаг 3: Дополнительная очистка ---
        final_answer = self._sanitize_answer(final_answer, primary_context_text)
    
        # --- Логируем диалог ---
        self.meta_agent.log_dialog(primary_agent.name, [], final_answer, query)
    
        return final_answer
    @monitor_resources
    def ask(self, question: str, max_tokens: int = 8000) -> str:
        return self.generate_answer_chat(question)


# ---------------------------
# Gradio UI с оценкой ответов
# ---------------------------

rag_system = RAGSystem()
print("🎉 RAG-система с обучением агентов и мультиагентным диалогом готова.")
conversation_state = {"stage": 0}

def respond(message: str, history: list, state: dict, rating: int = None) -> Tuple[str, list, dict, gr.update, gr.update]:
    new_history = history.copy()
    user_response = message.strip() if message else ""

    try:
        # Этап 0: Получение вопроса
        if state.get("stage", 0) == 0:
            needs_clarification, clarification_message, summary = rag_system.analyze_query_for_clarification(user_response)
            if needs_clarification and clarification_message:
                new_state = {
                    "stage": 1,
                    "original_query": user_response,
                    "summary": summary,
                    "clarification_question": clarification_message
                }
                new_history.append((message, clarification_message))
                return "", new_history, new_state, gr.update(visible=False), gr.update(visible=False)
            else:
                bot_message = rag_system.ask(summary or user_response)
                new_state = {"stage": 2, "last_query": summary or user_response, "last_answer": bot_message}
                new_history.append((message, bot_message))
                # Показываем рейтинг после ответа
                return "", new_history, new_state, gr.update(visible=True), gr.update(visible=True)

        # Этап 1: Уточнение вопроса
        elif state.get("stage") == 1:
            original_query = state.get("original_query", "")
            combined_query = f"{original_query} {user_response}"
            bot_message = rag_system.ask(combined_query)
            new_state = {"stage": 2, "last_query": combined_query, "last_answer": bot_message}
            new_history.append((message, f"📝 Уточнение: {user_response}"))
            new_history.append((message, bot_message))
            # Показываем рейтинг
            return "", new_history, new_state, gr.update(visible=True), gr.update(visible=True)

        # Этап 2: Получение рейтинга ИЛИ переход к новому вопросу
        elif state.get("stage") == 2:
            # Если пользователь поставил оценку — обрабатываем её
            if rating is not None:
                last_query = state.get("last_query", "")
                last_answer = state.get("last_answer", "")
                # Передаём фидбек агенту ТОЛЬКО если рейтинг >= 4
                if rating >= 4:
                    agent = rag_system.meta_agent.route(last_query)
                    if agent:
                        agent.add_feedback(last_query, last_answer, float(rating) / 5.0)  # нормализуем до 0.0-1.0
                        feedback_msg = f"🌟 Спасибо! Ваша оценка ({rating}/5) передана агенту '{agent.name}' для обучения."
                    else:
                        feedback_msg = f"🌟 Спасибо за высокую оценку ({rating}/5)!"
                else:
                    feedback_msg = "🙏 Спасибо за честную оценку. Мы постараемся стать лучше!"

                new_state = {"stage": 0}
                new_history.append(("Оценка", feedback_msg))
                # Скрываем рейтинг после отправки
                return "", new_history, new_state, gr.update(visible=False), gr.update(visible=False)

            # Если пользователь НЕ поставил оценку, но отправил НОВЫЙ вопрос — начинаем новый диалог
            elif message.strip():
                # Рекурсивно вызываем respond для нового вопроса, сбрасывая состояние
                return respond(message, history, {"stage": 0})

            # Если ни оценки, ни нового вопроса — оставляем всё как есть (теоретически не должно происходить)
            else:
                bot_message = "Пожалуйста, оцените предыдущий ответ или задайте новый вопрос."
                return message, new_history, state, gr.update(visible=True), gr.update(visible=True)

        # Любое другое непредусмотренное состояние — сбрасываем на этап 0
        else:
            new_state = {"stage": 0}
            # Рекурсивно вызываем обработку с чистого листа
            return respond(message, history, new_state)

    except Exception as e:
        bot_message = f"⚠️ Внутренняя ошибка: {e}"
        new_state = {"stage": 0}
        new_history.append((message, bot_message))
        return "", new_history, new_state, gr.update(visible=False), gr.update(visible=False)


# Создаём интерфейс
with gr.Blocks(title="RAG-Ассистент по ЖКХ с обучением и мультиагентностью") as demo:
    gr.Markdown("## 💬 Умный RAG-ассистент по ЖКХ")
    gr.Markdown("После ответа — поставьте оценку от 1 до 5 звёзд. Отзывы с **4 и 5 звёздами** используются для обучения агентов.")

    chatbot = gr.Chatbot(label="Диалог", bubble_full_width=False)
    msg = gr.Textbox(label="Ваш вопрос", placeholder="Введите текст...")
    send_button = gr.Button("Отправить")

    # Блок рейтинга (изначально скрыт)
    with gr.Row():
        rating_slider = gr.Slider(1, 5, step=1, value=5, label="Оцените ответ (1-5 звёзд)", visible=False)
        submit_rating = gr.Button("Отправить оценку", visible=False)

    clear = gr.ClearButton([msg, chatbot, rating_slider], value="Очистить")
    state = gr.State(value={"stage": 0})

    # Обработка отправки вопроса
    msg.submit(
        respond,
        inputs=[msg, chatbot, state],
        outputs=[msg, chatbot, state, rating_slider, submit_rating]
    )
    send_button.click(
        respond,
        inputs=[msg, chatbot, state],
        outputs=[msg, chatbot, state, rating_slider, submit_rating]
    )

    # Обработка отправки рейтинга
    submit_rating.click(
        respond,
        inputs=[msg, chatbot, state, rating_slider],
        outputs=[msg, chatbot, state, rating_slider, submit_rating]
    )

demo.launch(share=True, server_name="0.0.0.0", server_port=7860)
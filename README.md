
# Backend (FastAPI, KVM-only)

CloudGaming Backend — это FastAPI приложение для управления виртуальными машинами через libvirt с поддержкой SSH туннелей и видео потоков.

## Предварительные требования

### Системные зависимости
- Python 3.8+
- PostgreSQL (для базы данных)
- SSH клиент
- sshpass (для аутентификации по паролю)

На Linux:
```bash
sudo apt-get update
sudo apt-get install -y python3-pip postgresql postgresql-contrib sshpass libvirt-dev
```

## Установка

### 1. Клонирование репозитория
```bash
git clone <repository-url>
cd backend
```

### 2. Создание виртуального окружения
```bash
python3 -m venv venv
source venv/bin/activate  # На Windows: venv\Scripts\activate
```

### 3. Установка зависимостей Python
```bash
pip install -r requirements.txt
```

### 4. Настройка базы данных PostgreSQL
```bash
# Создание пользователя и базы данных
sudo -u postgres psql
CREATE USER cloudgaming WITH PASSWORD 'your_password';
CREATE DATABASE cloudgaming_db OWNER cloudgaming;
ALTER ROLE cloudgaming SET client_encoding TO 'utf8';
ALTER ROLE cloudgaming SET default_transaction_isolation TO 'read committed';
ALTER ROLE cloudgaming SET default_transaction_deferrable TO on;
ALTER ROLE cloudgaming SET default_transaction_read_committed TO on;
GRANT ALL PRIVILEGES ON DATABASE cloudgaming_db TO cloudgaming;
\q
```


### 5. Инициализация базы данных
```bash
python init_db.py
```

Это создаст все необходимые таблицы и схемы.

## Запуск приложения

### Разработка
```bash
uvicorn main:app --host 0.0.0.0 --port 9920 --reload
```



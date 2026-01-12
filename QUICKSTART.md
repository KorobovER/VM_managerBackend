# Быстрый старт CloudGaming Backend

## Шаг 1: Подготовка окружения

### 1.1 Установка PostgreSQL

**Windows:**
- Скачайте установщик с https://www.postgresql.org/download/windows/
- Установите с параметрами по умолчанию
- Запомните пароль для пользователя `postgres`

**Linux (Ubuntu/Debian):**
```bash
sudo apt-get update
sudo apt-get install postgresql postgresql-contrib
sudo systemctl start postgresql
```

**macOS:**
```bash
brew install postgresql
brew services start postgresql
```

### 1.2 Создание БД и пользователя

```bash
# Подключитесь к PostgreSQL
psql -U postgres

# В интерпретаторе psql выполните:
CREATE DATABASE cloudgaming;
CREATE USER cloudgaming_user WITH PASSWORD 'cloudgaming_pass';
ALTER ROLE cloudgaming_user SET client_encoding TO 'utf8';
ALTER ROLE cloudgaming_user SET default_transaction_isolation TO 'read committed';
ALTER ROLE cloudgaming_user SET default_transaction_deferrable TO on;
GRANT ALL PRIVILEGES ON DATABASE cloudgaming TO cloudgaming_user;
\q
```

> **Примечание:** Таблицы будут созданы автоматически при первом запуске приложения. Вам не нужно запускать миграции вручную!

## Шаг 2: Установка зависимостей

```bash
# Перейдите в папку проекта
cd backend

# Установите зависимости
pip install -r requirements.txt
```

## Шаг 3: Конфигурация

### 3.1 Отредактируйте `config.json`

Откройте `config.json` и установите параметры подключения к БД:

```json
{
  "db": {
    "host": "localhost",
    "port": 5432,
    "db": "cloudgaming",
    "user": "cloudgaming_user",
    "password": "cloudgaming_pass"
  },
  ...
}
```

Измените значения `host`, `port`, `user`, `password` если нужно.

## Шаг 4: Запуск приложения

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

При первом запуске приложение автоматически создаст все необходимые таблицы в БД.

Приложение будет доступно по адресу: `http://localhost:8000`

## Шаг 5: Инициализация тестовых данных (опционально)

Если нужны тестовые пользователи и серверы, запустите в отдельном терминале:

```bash
python init_db.py
```

Вывод должен быть:
```
✓ Created admin user (username: root, password: root)
✓ Created test user (username: user, password: user)
✓ Created default jump host
✓ Created VM Host 1
✓ Created VM Host 2

✓ Database initialized successfully!
```

## Шаг 6: Тестирование API

### 6.1 Вход в систему

```bash
curl -X POST "http://localhost:8000/auth/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=root&password=root"
```

Ответ:
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer"
}
```

### 6.2 Получить информацию о текущем пользователе

```bash
TOKEN="your_access_token"

curl -X GET "http://localhost:8000/auth/me" \
  -H "Authorization: Bearer $TOKEN"
```

### 6.3 Получить список серверов

```bash
curl -X GET "http://localhost:8000/servers" \
  -H "Authorization: Bearer $TOKEN"
```

### 6.4 Получить список ВМ

```bash
curl -X GET "http://localhost:8000/vms" \
  -H "Authorization: Bearer $TOKEN"
```

## Структура папок

```
backend/
├── db/                      # Папка с файлами БД
│   ├── __init__.py
│   ├── database.py          # Конфигурация подключения
│   ├── models.py            # SQLAlchemy модели
│   └── schemas.py           # Pydantic схемы
├── main.py                  # Основное приложение FastAPI
├── auth.py                  # Аутентификация и авторизация
├── init_db.py               # Инициализация БД
├── config.json              # Конфигурация БД, jump host и VM hosts
├── config.yaml              # Конфигурация jump host и VM hosts (legacy)
└── requirements.txt         # Зависимости Python
```

## Учетные данные по умолчанию

### Администратор (создаётся при инициализации)
- **Username:** root
- **Password:** root
- **Email:** root@root.root

### Тестовый пользователь (создаётся при инициализации)
- **Username:** user
- **Password:** user
- **Email:** user@cloudgaming.local

## Основные возможности

### Для администраторов
- ✓ Создание и управление серверами
- ✓ Создание и управление ВМ
- ✓ Полный доступ ко всем API

### Для пользователей
- ✓ Запуск и остановка ВМ
- ✓ Подключение к видеопотокам
- ✓ Запуск и остановка proxy для видео
- ✓ Просмотр информации о серверах и ВМ

## Решение проблем

### Ошибка: "could not connect to server"
```
Убедитесь, что PostgreSQL запущен:
- Windows: Services > PostgreSQL
- Linux: sudo systemctl start postgresql
- macOS: brew services start postgresql
```

### Ошибка: "database does not exist"
```
Запустите команду создания БД еще раз:
psql -U postgres -c "CREATE DATABASE cloudgaming;"
```

### Ошибка: "role does not exist"
```
Создайте пользователя:
psql -U postgres -c "CREATE USER cloudgaming_user WITH PASSWORD 'cloudgaming_pass';"
```

### Ошибка: "permission denied"
```
Выдайте права:
psql -U postgres -c "GRANT ALL PRIVILEGES ON DATABASE cloudgaming TO cloudgaming_user;"
```

### Таблицы не созданы автоматически
```
Убедитесь, что приложение запущено (таблицы создаются при старте):
uvicorn main:app --reload

Если проблема сохраняется, проверьте права доступа пользователя cloudgaming_user к БД.
```

## Следующие шаги

1. Отредактируйте `config.yaml` для добавления своих jump hosts и VM hosts
2. Создайте дополнительных пользователей через API
3. Добавьте новые серверы и ВМ
4. Настройте SSL/TLS для production
5. Установите более сильный `SECRET_KEY`


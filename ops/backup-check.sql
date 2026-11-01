-- Контрольные строки для автопроверки восстановления.
-- Таблицы с заведомо непустыми данными на 30.10.2026: подписки, платежи, задачи.
SELECT count(*) FROM subscriptions;
SELECT count(*) FROM payments;
SELECT count(*) FROM jobs;

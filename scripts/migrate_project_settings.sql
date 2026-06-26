CREATE TABLE IF NOT EXISTS project_settings (
    id INT PRIMARY KEY,
    rules_text TEXT NULL,
    rules_url VARCHAR(512) NULL,
    updated_at DATETIME NULL
);

-- Run once. Safe to re-run: skips columns that already exist (MySQL 8.0).

-- stage_results
SET @db = DATABASE();

SET @sql = IF(
    (SELECT COUNT(*) FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'stage_results' AND COLUMN_NAME = 'econ_rating') = 0,
    'ALTER TABLE stage_results ADD COLUMN econ_rating INT NULL AFTER acs',
    'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = IF(
    (SELECT COUNT(*) FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'stage_results' AND COLUMN_NAME = 'first_bloods') = 0,
    'ALTER TABLE stage_results ADD COLUMN first_bloods INT NULL AFTER econ_rating',
    'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = IF(
    (SELECT COUNT(*) FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'stage_results' AND COLUMN_NAME = 'spikes_planted') = 0,
    'ALTER TABLE stage_results ADD COLUMN spikes_planted INT NULL AFTER first_bloods',
    'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = IF(
    (SELECT COUNT(*) FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'stage_results' AND COLUMN_NAME = 'spikes_defused') = 0,
    'ALTER TABLE stage_results ADD COLUMN spikes_defused INT NULL AFTER spikes_planted',
    'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- finalists
SET @sql = IF(
    (SELECT COUNT(*) FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'finalists' AND COLUMN_NAME = 'participation_confirmed') = 0,
    'ALTER TABLE finalists ADD COLUMN participation_confirmed TINYINT(1) NOT NULL DEFAULT 0 AFTER source',
    'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql = IF(
    (SELECT COUNT(*) FROM information_schema.COLUMNS
     WHERE TABLE_SCHEMA = @db AND TABLE_NAME = 'finalists' AND COLUMN_NAME = 'participation_confirmed_at') = 0,
    'ALTER TABLE finalists ADD COLUMN participation_confirmed_at DATETIME NULL AFTER participation_confirmed',
    'SELECT 1'
);
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

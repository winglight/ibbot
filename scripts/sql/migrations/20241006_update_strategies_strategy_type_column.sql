-- Ensure the strategies table uses `strategy_type` instead of `factory`
-- and that dependent columns exist with the expected ordering.

-- Detect whether the new `strategy_type` column already exists.
SET @has_strategy_type = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'strategies'
      AND COLUMN_NAME = 'strategy_type'
);

-- Detect whether the legacy `factory` column exists.
SET @has_factory = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'strategies'
      AND COLUMN_NAME = 'factory'
);

-- Rename the legacy column when present, otherwise create the new column.
SET @sql = CASE
    WHEN @has_strategy_type = 0 AND @has_factory > 0 THEN
        'ALTER TABLE `strategies` CHANGE COLUMN `factory` `strategy_type` VARCHAR(255) NULL AFTER `name`'
    WHEN @has_strategy_type = 0 THEN
        'ALTER TABLE `strategies` ADD COLUMN `strategy_type` VARCHAR(255) NULL AFTER `name`'
    ELSE
        'SELECT "strategy_type column already exists" AS message'
END;
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- Ensure the params column exists immediately after strategy_type.
SET @has_params = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'strategies'
      AND COLUMN_NAME = 'params'
);
SET @sql = CASE
    WHEN @has_params = 0 THEN
        'ALTER TABLE `strategies` ADD COLUMN `params` JSON NULL AFTER `strategy_type`'
    ELSE
        'SELECT "params column already exists" AS message'
END;
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- Ensure the metadata column exists immediately after params.
SET @has_metadata = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'strategies'
      AND COLUMN_NAME = 'metadata'
);
SET @sql = CASE
    WHEN @has_metadata = 0 THEN
        'ALTER TABLE `strategies` ADD COLUMN `metadata` JSON NULL AFTER `params`'
    ELSE
        'SELECT "metadata column already exists" AS message'
END;
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- Ensure the enabled column exists.
SET @has_enabled = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'strategies'
      AND COLUMN_NAME = 'enabled'
);
SET @sql = CASE
    WHEN @has_enabled = 0 THEN
        'ALTER TABLE `strategies` ADD COLUMN `enabled` TINYINT(1) NOT NULL DEFAULT 1 AFTER `metadata`'
    ELSE
        'SELECT "enabled column already exists" AS message'
END;
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- Ensure the enabled_at column exists.
SET @has_enabled_at = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'strategies'
      AND COLUMN_NAME = 'enabled_at'
);
SET @sql = CASE
    WHEN @has_enabled_at = 0 THEN
        'ALTER TABLE `strategies` ADD COLUMN `enabled_at` DATETIME NULL AFTER `enabled`'
    ELSE
        'SELECT "enabled_at column already exists" AS message'
END;
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- Ensure the disabled_at column exists.
SET @has_disabled_at = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'strategies'
      AND COLUMN_NAME = 'disabled_at'
);
SET @sql = CASE
    WHEN @has_disabled_at = 0 THEN
        'ALTER TABLE `strategies` ADD COLUMN `disabled_at` DATETIME NULL AFTER `enabled_at`'
    ELSE
        'SELECT "disabled_at column already exists" AS message'
END;
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- Normalise description column type.
ALTER TABLE `strategies` MODIFY `description` TEXT NULL;

SELECT 'Migration completed: strategies table updated with strategy_type column' AS result;

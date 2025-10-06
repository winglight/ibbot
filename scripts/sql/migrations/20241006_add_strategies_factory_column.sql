-- Add missing factory column and other required columns to strategies table
-- This migration ensures the strategies table has all required columns as defined in the schema

-- Check if factory column exists, if not add it
SET @column_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'strategies'
    AND COLUMN_NAME = 'factory'
);

-- Add factory column if it doesn't exist
SET @sql = IF(@column_exists = 0,
    'ALTER TABLE `strategies` ADD COLUMN `factory` VARCHAR(255) NULL AFTER `name`',
    'SELECT "factory column already exists" AS message'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- Check if params column exists, if not add it
SET @column_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'strategies'
    AND COLUMN_NAME = 'params'
);

-- Add params column if it doesn't exist
SET @sql = IF(@column_exists = 0,
    'ALTER TABLE `strategies` ADD COLUMN `params` JSON NULL AFTER `factory`',
    'SELECT "params column already exists" AS message'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- Check if metadata column exists, if not add it
SET @column_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'strategies'
    AND COLUMN_NAME = 'metadata'
);

-- Add metadata column if it doesn't exist
SET @sql = IF(@column_exists = 0,
    'ALTER TABLE `strategies` ADD COLUMN `metadata` JSON NULL AFTER `params`',
    'SELECT "metadata column already exists" AS message'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- Check if enabled column exists, if not add it
SET @column_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'strategies'
    AND COLUMN_NAME = 'enabled'
);

-- Add enabled column if it doesn't exist
SET @sql = IF(@column_exists = 0,
    'ALTER TABLE `strategies` ADD COLUMN `enabled` TINYINT(1) NOT NULL DEFAULT 1 AFTER `metadata`',
    'SELECT "enabled column already exists" AS message'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- Check if enabled_at column exists, if not add it
SET @column_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'strategies'
    AND COLUMN_NAME = 'enabled_at'
);

-- Add enabled_at column if it doesn't exist
SET @sql = IF(@column_exists = 0,
    'ALTER TABLE `strategies` ADD COLUMN `enabled_at` DATETIME NULL AFTER `enabled`',
    'SELECT "enabled_at column already exists" AS message'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- Check if disabled_at column exists, if not add it
SET @column_exists = (
    SELECT COUNT(*)
    FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 'strategies'
    AND COLUMN_NAME = 'disabled_at'
);

-- Add disabled_at column if it doesn't exist
SET @sql = IF(@column_exists = 0,
    'ALTER TABLE `strategies` ADD COLUMN `disabled_at` DATETIME NULL AFTER `enabled_at`',
    'SELECT "disabled_at column already exists" AS message'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- Ensure description column is TEXT type (modify if needed)
ALTER TABLE `strategies` MODIFY `description` TEXT NULL;

-- Remove deprecated columns if they exist (commented out for safety)
-- ALTER TABLE `strategies` DROP COLUMN IF EXISTS `strategy_type`;
-- ALTER TABLE `strategies` DROP COLUMN IF EXISTS `api_endpoint`;
-- ALTER TABLE `strategies` DROP COLUMN IF EXISTS `file_path`;

SELECT 'Migration completed: strategies table updated with required columns' AS result;
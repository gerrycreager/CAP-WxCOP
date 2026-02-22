-- Add is_military column to airports table
-- Run with: psql -U avwx_user -d avwx_data -f add_is_military_column.sql

-- Add the column
ALTER TABLE observations.airports 
ADD COLUMN IF NOT EXISTS is_military BOOLEAN DEFAULT false;

-- Create index for efficient filtering
CREATE INDEX IF NOT EXISTS idx_airports_military ON observations.airports(is_military) 
WHERE is_military = true;

-- Show the updated table structure
\d observations.airports


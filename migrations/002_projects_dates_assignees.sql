-- Add start date, deadline, and support assignees via project_members
ALTER TABLE projects ADD COLUMN IF NOT EXISTS start_date DATE;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS deadline DATE;

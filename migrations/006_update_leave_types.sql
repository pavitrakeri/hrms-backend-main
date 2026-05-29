-- Migration to update leave types to 12 sick and 12 casual days, removing other types.
DELETE FROM leave_balance WHERE leave_type_id NOT IN (1, 2);
DELETE FROM leaves WHERE leave_type_id NOT IN (1, 2);
DELETE FROM leave_types WHERE id NOT IN (1, 2);

INSERT INTO leave_types (id, name, default_days) VALUES 
    (1, 'sick', 12),
    (2, 'casual', 12)
ON CONFLICT (id) DO UPDATE 
SET name = EXCLUDED.name, default_days = EXCLUDED.default_days;

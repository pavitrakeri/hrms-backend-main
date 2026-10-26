-- Migration 014: Attendance Regularisation & Enhanced Attendance Tracking

-- 1. Add is_regularised column to attendance table if not exists
ALTER TABLE attendance ADD COLUMN IF NOT EXISTS is_regularised BOOLEAN DEFAULT false;

-- 2. Create attendance_regularisations table
CREATE TABLE IF NOT EXISTS attendance_regularisations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    attendance_id UUID REFERENCES attendance(id) ON DELETE SET NULL,
    request_date DATE NOT NULL,
    requested_clock_in TIMESTAMP WITH TIME ZONE NOT NULL,
    requested_clock_out TIMESTAMP WITH TIME ZONE NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', -- 'pending', 'approved', 'rejected'
    rejection_reason TEXT,
    reviewed_by UUID REFERENCES users(id) ON DELETE SET NULL,
    reviewed_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

-- 3. Indexes for fast filtering
CREATE INDEX IF NOT EXISTS idx_att_reg_user_id ON attendance_regularisations(user_id);
CREATE INDEX IF NOT EXISTS idx_att_reg_status ON attendance_regularisations(status);
CREATE INDEX IF NOT EXISTS idx_att_reg_request_date ON attendance_regularisations(request_date);

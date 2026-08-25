BEGIN;

-- Indexes for DOU
CREATE INDEX IF NOT EXISTS idx_dou_job_obs_job_time ON public.dou_job_observations(dou_job_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_dou_job_obs_run_id ON public.dou_job_observations(run_id);

-- Indexes for HappyMonday
CREATE INDEX IF NOT EXISTS idx_hm_job_obs_job_time ON public.hm_job_observations(hm_job_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_hm_job_obs_run_id ON public.hm_job_observations(run_id);

COMMIT;
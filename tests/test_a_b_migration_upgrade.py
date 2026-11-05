import json
import os
from pathlib import Path
import subprocess
import sys

from sqlalchemy import create_engine, text


def test_upgrade_from_deployed_a_preserves_question_history_and_evaluation(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'DATABASE_URL': f'sqlite:///{tmp_path / "a-upgrade.db"}'}
    subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', '20261105_08'], cwd=root, env=env, check=True)
    engine = create_engine(env['DATABASE_URL'])
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, login_id, name, password_hash, created_at) VALUES ('u','owner','Owner','hash','2026-11-05')"))
        conn.execute(text("INSERT INTO projects (id, owner_id, name, revision, created_at, updated_at) VALUES ('p','u','Project',1,'2026-11-05','2026-11-05')"))
        conn.execute(text("INSERT INTO services (id, project_id, name, revision, created_at, updated_at) VALUES ('s','p','Service',1,'2026-11-05','2026-11-05')"))
        conn.execute(text("INSERT INTO testsets (id,project_id,name,revision,created_at) VALUES ('ts','p','Questions',1,'2026-11-05')"))
        conn.execute(text("INSERT INTO testset_versions (id,testset_id,version,revision,status,policy_version_ids,created_at) VALUES ('tv','ts',1,1,'draft','[]','2026-11-05')"))
        conn.execute(text("INSERT INTO question_history (id,testset_version_id,question_id,version_revision,action,actor_id,after,created_at) VALUES ('h','tv','q',1,'created','u',:payload,'2026-11-05')"), {'payload': json.dumps({'conditionId': 'stable-condition'})})
        conn.execute(text("INSERT INTO evaluations (id,service_id,request_hash,inputs,status,total_questions,completed_questions,failed_questions,created_at) VALUES ('e','s','request','{}','COMPLETED',1,1,0,'2026-11-05')"))
        conn.execute(text("INSERT INTO evaluation_items (id,evaluation_id,question_id,input,status,a,created_at) VALUES ('i','e','q','{}','COMPLETED',:answer,'2026-11-05')"), {'answer': json.dumps({'answer': '기존 답변'})})
    subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], cwd=root, env=env, check=True)
    subprocess.run([sys.executable, '-m', 'alembic', 'check'], cwd=root, env=env, check=True)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT version_num FROM alembic_version')).scalar_one() == '20261105_10'
        assert json.loads(conn.execute(text("SELECT after FROM question_history WHERE id='h'")).scalar_one())['conditionId'] == 'stable-condition'
        answer, revision = conn.execute(text("SELECT a,review_revision FROM evaluation_items WHERE id='i'")).one()
        assert json.loads(answer)['answer'] == '기존 답변' and revision == 0
    engine.dispose()

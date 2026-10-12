"""Offline rendering of recorded demo states; no physics advancement."""
import os
os.environ.setdefault('MUJOCO_GL','egl')
import subprocess
from pathlib import Path
import numpy as np
from .io import read,write


def render_demo(attempt,out):
    import mujoco
    attempt=Path(attempt);out=Path(out);out.mkdir(parents=True,exist_ok=False)
    m=mujoco.MjModel.from_xml_path(str((attempt/'scene.xml').resolve()));d=mujoco.MjData(m);data=np.load(attempt/'states.npz')['samples']
    renderer=mujoco.Renderer(m,height=480,width=640);top=float(read(attempt.parent.parent/'task.json')['scene'].get('table_height',.65))
    ids=list(range(3,len(data),4))
    if not ids or ids[-1]!=len(data)-1:ids.append(len(data)-1)
    outputs=[]
    for name,azimuth,elevation in [('external',110,-28),('tool_view',90,-60)]:
        camera=mujoco.MjvCamera();camera.lookat[:]=[0,0,top+.07];camera.distance=.9;camera.azimuth=azimuth;camera.elevation=elevation
        target=out/f'{name}.mp4';process=subprocess.Popen(['ffmpeg','-v','error','-f','rawvideo','-pixel_format','rgb24','-video_size','640x480','-framerate','50','-i','-','-an','-c:v','libx264','-crf','18','-pix_fmt','yuv420p',str(target)],stdin=subprocess.PIPE)
        try:
            for i in ids:
                d.qpos[:]=data[i,1:1+m.nq];d.qvel[:]=data[i,1+m.nq:1+m.nq+m.nv];mujoco.mj_forward(m,d)
                renderer.update_scene(d,camera);frame=renderer.render();process.stdin.write(frame.tobytes())
            process.stdin.close()
            if process.wait(timeout=60):raise RuntimeError('Video encode failed')
        finally:
            if process.poll() is None:process.kill();process.wait()
        subprocess.run(['ffmpeg','-v','error','-i',str(target),'-f','null','-'],check=True,timeout=60);outputs.append(str(target))
    renderer.close();report={'videos':outputs,'source_indices':ids,'source_times':[float(data[i,0]) for i in ids],'fps':50,'physics_steps':0,'camera_note':'Demo has no humanoid head: second view is overhead/tool overview, not a claimed robot first-person camera.','training_ready':False}
    write(out/'verification.json',report);return report

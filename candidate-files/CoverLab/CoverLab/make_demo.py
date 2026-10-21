"""Create original synthetic control tones; no singer voice or existing song."""
from pathlib import Path
import math,struct,uuid,wave

def create_tone(path, frequency=220, duration=6, gain=0.18, rate=44100, channels=1):
    with wave.open(str(path), 'wb') as f:
        f.setparams((channels,2,rate,0,'NONE','not compressed'))
        b=bytearray()
        for i in range(round(duration*rate)):
            fade=min(1,i/(rate*.025),(duration*rate-i)/(rate*.025))
            value=int(32767*gain*max(0,fade)*(math.sin(2*math.pi*frequency*i/rate)+.15*math.sin(4*math.pi*frequency*i/rate)))
            b.extend(struct.pack('<h',value)*channels)
        f.writeframes(b)
    return path

def create_demo_audio(directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    result=[]
    for name,freq,gain in [('合成控制音-原音220Hz.wav',220,.18),('合成控制音-伴奏330Hz.wav',330,.12),('合成控制音-对照人声位440Hz.wav',440,.18)]:
        p=create_tone(directory/(uuid.uuid4().hex+'.wav'),freq,gain=gain)
        result.append((p,name))
    (directory/'合成演示.lrc').write_text('[00:00.00]合成控制音，不是歌手或歌曲\n[00:01.50]用于试听、截取与移调检查\n[00:03.00]音色翻唱尚未接入\n[00:04.50]原音文件始终保留\n',encoding='utf8')
    return result

if __name__=='__main__':
    from core import AudioWorkspace
    print(AudioWorkspace(Path(__file__).resolve().parent).install_demo())

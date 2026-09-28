"""Render the illustrated README voice-to-Harness flow (Pillow, no API calls).

Usage: hal/.venv/bin/python docs/architecture/animate_voice_flow.py
Fonts: system Arial on macOS, DejaVu Sans on Linux, or --font-dir with
Arial.ttf, Arial Bold.ttf and Courier New.ttf. Output is illustrative, not a
screen recording, execution trace or latency benchmark.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
W, H, SCALE, FPS = 1100, 660, 2, 10
DURATION = 21
BG = '#10151b'
PANEL = '#171f28'
NODE = '#1e2935'
LINE = '#344251'
TEXT = '#f1f5f9'
MUTED = '#9daebe'
TEAL = '#77e2cf'
GOLD = '#ffd16e'
PURPLE = '#b5a7f6'
GREEN = '#9ce2a4'
STAGES = [
    (0, 3, 'Speak', 'A request starts with your voice.',
     '“Ask my Blender agent to lift the yellow planes.”'),
    (3, 5.5, 'Route', 'Realtime understands. The task goes to main.',
     'delegate_to_main → OS server'),
    (5.5, 8, 'Delegate', 'The runtime picks a skill and a computer agent.',
     'harness-use → paired computer · authenticated connection'),
    (8, 12.5, 'Work', 'The computer agent makes the change.',
     'The scene updates. Progress stays in the UI, not on the speaker.'),
    (12.5, 16, 'Summarize', 'A detailed result comes back. HAL prepares the spoken summary.',
     'Result: three planes raised; scene exported; preview and turntable refreshed.'),
    (16, 21, 'Reply', 'You hear the outcome. The work is already done.',
     '“Done. The yellow planes are higher.”'),
]


def ease(x):
    x = max(0.0, min(1.0, x))
    return x*x*(3-2*x)


def blend(a, b, f):
    a, b = a.lstrip('#'), b.lstrip('#')
    return tuple(round(int(a[i:i+2],16)*(1-f)+int(b[i:i+2],16)*f) for i in (0,2,4))


class Renderer:
    def __init__(self, font_dir=None):
        mac = Path('/System/Library/Fonts/Supplemental')
        linux = Path('/usr/share/fonts/truetype/dejavu')
        if font_dir or mac.exists():
            root = font_dir or mac
            self.paths = [root/'Arial.ttf', root/'Arial Bold.ttf', root/'Courier New.ttf']
        else:
            self.paths = [linux/'DejaVuSans.ttf', linux/'DejaVuSans-Bold.ttf', linux/'DejaVuSansMono.ttf']
        self.fonts = {}

    def font(self, size, weight=0):
        key=(size, weight)
        if key not in self.fonts:
            self.fonts[key]=ImageFont.truetype(str(self.paths[weight]), round(size*SCALE))
        return self.fonts[key]

    def text(self, xy, text, size=16, color=TEXT, weight=0, anchor=None):
        self.d.text(tuple(v*SCALE for v in xy), text, font=self.font(size, weight),
                    fill=color, anchor=anchor, stroke_width=0)

    def rect(self, box, fill=PANEL, outline=None, radius=14, width=1):
        self.d.rounded_rectangle(tuple(round(v*SCALE) for v in box), radius=radius*SCALE,
                                fill=fill, outline=outline, width=width*SCALE)

    def line(self, pts, fill=LINE, width=2):
        self.d.line([(round(x*SCALE),round(y*SCALE)) for x,y in pts], fill=fill, width=width*SCALE, joint='curve')

    def circle(self, x,y,r, fill, outline=None,width=1):
        self.d.ellipse(tuple(round(v*SCALE) for v in (x-r,y-r,x+r,y+r)), fill=fill, outline=outline,width=width*SCALE)

    def polygon(self, pts, fill):
        self.d.polygon([(round(x*SCALE),round(y*SCALE)) for x,y in pts],fill=fill)

    def path(self, points, color, active=False, progress=0.0):
        self.line(points, blend(LINE,color,.5) if active else LINE, 2)
        x,y=points[-1]; px,py=points[-2];angle=math.atan2(y-py,x-px)
        self.polygon([(x,y),(x-7*math.cos(angle-.5),y-7*math.sin(angle-.5)),
                      (x-7*math.cos(angle+.5),y-7*math.sin(angle+.5))],color if active else LINE)
        if not active:return
        lengths=[math.hypot(b[0]-a[0],b[1]-a[1]) for a,b in zip(points,points[1:])]
        total=sum(lengths)
        for offset in (0,.34,.68):
            target=((progress+offset)%1)*total
            for (a,b),length in zip(zip(points,points[1:]),lengths):
                if target<=length:
                    f=target/length if length else 0;x=a[0]+(b[0]-a[0])*f;y=a[1]+(b[1]-a[1])*f
                    self.circle(x,y,7,blend(PANEL,color,.12));self.circle(x,y,3,color);break
                target-=length

    def waveform(self,x,y,t,active,color=TEAL,n=23,width=4,gap=7):
        for i in range(n):
            strength=(math.sin(i*.73+t*9)+1)/2
            envelope=math.sin(math.pi*(i+1)/(n+1))
            h=3+(25*strength*envelope if active else 2*envelope)
            self.rect((x+i*gap,y-h,x+i*gap+width,y+h),color if active else LINE,radius=2)

    def plane(self,cx,cy,size,color,ghost=False):
        points=[(0,-.68),(.12,-.09),(.60,.21),(.58,.33),(.13,.21),(.10,.57),
                (.25,.72),(.23,.81),(0,.68),(-.23,.81),(-.25,.72),(-.10,.57),
                (-.13,.21),(-.58,.33),(-.60,.21),(-.12,-.09)]
        # Rotate the top-down silhouette into an isometric viewport.
        def project(x,y):return cx+(x*.9-y*.55)*size,cy+(x*.35+y*.55)*size
        verts=[project(x,y) for x,y in points]
        self.polygon(verts, color)
        if not ghost:
            self.line([project(0,-.55),project(0,.62)],blend(color,'#ffffff',.4),1)
            x,y=project(0,-.13);self.circle(x,y,2,blend(color,BG,.65))

    def node(self,y,title,detail,code,color,active,done):
        fill=blend(NODE,color,.06) if active else NODE
        self.rect((286,y,619,y+88),fill,color if active else LINE,width=2 if active else 1)
        self.circle(302,y+21,3,color if active or done else MUTED)
        self.text((314,y+11),title,17,TEXT,1)
        self.text((302,y+36),detail,13,MUTED)
        self.text((302,y+60),code,12,color if active or done else MUTED,2)
        if done:
            self.line([(591,y+20),(595,y+24),(602,y+16)],GREEN,2)

    def frame(self,t):
        self.im=Image.new('RGB',(W*SCALE,H*SCALE),BG);self.d=ImageDraw.Draw(self.im)
        stage=next(i for i,(a,b,*_) in enumerate(STAGES) if a<=t<b)
        a,b,label,headline,caption=STAGES[stage]
        f=(t-a)/(b-a)
        # Quiet grid gives the diagram a technical canvas without moving text.
        for x in range(32,W,24):
            for y in range(24,H,24):self.circle(x,y,.6,'#26313c')
        self.text((32,24),'AUTONOMOUS OS',13,TEAL,1)
        self.text((32,49),'From voice to a finished task.',31,TEXT,1)
        self.text((1068,29),'ILLUSTRATED FLOW',11,MUTED,2,anchor='ra')
        self.text((1068,53),'Time compressed · not a latency benchmark',11,MUTED,anchor='ra')

        # The three zones remain fixed throughout the story.
        self.rect((32,112,244,510),PANEL,LINE)
        self.rect((268,112,637,510),PANEL,LINE)
        self.rect((663,112,1068,510),PANEL,LINE)
        for x,num,name in [(48,'01','YOUR DEVICE'),(286,'02','OS ORCHESTRATION'),(681,'03','YOUR COMPUTER')]:
            self.text((x,132),num,11,TEAL,2);self.text((x+28,130),name,12,MUTED,1)

        # Device is intentionally a generic body, not a Lamp-only product.
        self.rect((70,186,206,348),'#0b1118','#465666',radius=29,width=2)
        self.rect((84,201,192,321),'#121e28',radius=18)
        self.circle(138,252,36,blend('#121e28',TEAL,.07),TEAL if stage in (0,5) else LINE)
        pulse=2*math.sin(t*5) if stage in (0,5) else 0
        self.circle(126,246,4+pulse*.3,TEAL);self.circle(150,246,4+pulse*.3,TEAL)
        self.line([(126,267),(133,270),(143,270),(150,267)],TEAL,2)
        self.rect((109,348,167,362),'#293745',radius=6)
        self.rect((90,359,186,370),'#344453',radius=5)
        status=['LISTENING','ROUTING','DELEGATED','WORKING','RESULT READY','SPEAKING'][stage]
        color=TEAL if stage in (0,1,5) else GOLD if stage in (3,4) else PURPLE
        self.text((138,391),status,12,color,1,anchor='mm')
        self.waveform(59,439,t,stage in (0,5),color,n=23)
        self.text((138,480),'Microphone + speaker',12,MUTED,anchor='mm')

        self.node(175,'HAL · realtime voice','Understand the spoken request',
                  'delegate_to_main' if stage>=1 else 'audio → realtime model',TEAL,stage in (0,1),stage>1)
        self.node(300,'OS · main runtime','Choose the skill and task owner',
                  'harness-use → computer agent' if stage>=2 else 'Hermes / OpenClaw / …',PURPLE,stage in (2,3),stage>3)
        self.node(411,'HAL · announcer','Final result → short spoken reply',
                  'realtime voice or summarizer + TTS',GOLD,stage in (4,5),False)

        self.path([(244,219),(286,219)],TEAL,stage in (0,1),t*.65)
        self.path([(452,263),(452,300)],TEAL,stage in (1,2),t*.7)
        self.path([(619,344),(663,344)],PURPLE,stage in (2,3),t*.55)
        self.path([(663,455),(619,455)],GOLD,stage==4,-t*.55)
        self.path([(286,455),(244,455)],TEAL,stage==5,-t*.65)
        self.text((454,280),'delegate',10,MUTED,2,anchor='mm')

        # A small, real change in the illustrated workspace makes the outcome legible.
        self.rect((680,171,1051,420),'#101820','#354354',radius=8)
        self.rect((680,171,1051,204),'#243140',radius=8)
        for i,c in enumerate(('#df8780','#eac979','#83c594')):self.circle(694+i*12,186,3,c)
        self.text((744,178),'Blender · scene preview',12,TEXT,2)
        for i in range(8):
            self.line([(686+i*49,413),(866+(i-3.5)*20,272)],'#273441',1)
        for yy in [295,310,329,354,385,413]:self.line([(686,yy),(1045,yy)],'#273441',1)
        raised=ease((t-8.5)/3.3)
        for x,y,z in [(775,335,43),(862,359,48),(950,335,43)]:
            self.plane(x,y+34,z,'#1e2933',True)
            if raised>.05:self.plane(x,y,z,'#35404a',True)
            self.plane(x,y-70*raised,z,GOLD if stage>=2 else '#c6a760')
        if raised>.05:
            self.line([(1021,340),(1021,340-70*raised)],GOLD,1)
            self.polygon([(1017,274),(1025,274),(1021,268)],GOLD)
        self.text((694,219),'Three yellow planes',12,MUTED)
        if raised>.9:self.text((1029,239),'RAISED',11,GOLD,1,anchor='ra')
        state='WAITING' if stage<2 else 'ACCEPTED' if stage==2 else 'EDITING SCENE' if stage==3 else 'COMPLETED'
        self.circle(695,443,3,GREEN if stage>=4 else PURPLE if stage>=2 else MUTED)
        self.text((706,435),state,11,GREEN if stage>=4 else MUTED,1)
        if stage<4:
            detail='Paired via Harness' if stage<2 else 'Task receipt received' if stage==2 else 'Update geometry → refresh preview'
            self.text((694,468),detail,12,MUTED)
        else:
            self.text((694,466),'turn.summary → OS → HAL',12,GOLD,2)
            self.text((694,486),'Full result stays in the app.',11,MUTED)

        # A stage rail lets a reader join the loop halfway through.
        for i,(_,_,name,*_) in enumerate(STAGES):
            x=40+i*174
            self.line([(x,531),(x+150,531)],TEAL if i<stage else LINE,3)
            if i==stage:self.line([(x,531),(x+150*f,531)],TEAL,3)
            self.text((x,542),f'{i+1:02}  {name}',12,TEAL if i==stage else MUTED,1 if i==stage else 0)
        self.rect((32,574,1068,640),'#1a2831',TEAL if stage in (0,5) else '#354653',radius=12)
        self.text((50,584),headline,13,MUTED)
        self.text((50,607),caption,19 if stage in (0,5) else 15,TEAL if stage==5 else TEXT,1 if stage in (0,5) else 0)
        return self.im.resize((W,H),Image.Resampling.LANCZOS)


def render(font_dir=None):
    r=Renderer(font_dir)
    # One palette shared across all frames keeps stationary glyphs from flickering.
    samples=[r.frame(t) for t in (1,4,6.5,10,14,18)]
    sheet=Image.new('RGB',(W,H*len(samples)))
    for i,im in enumerate(samples):sheet.paste(im,(0,H*i))
    palette=sheet.quantize(colors=240)
    frames=[]
    for n in range(DURATION*FPS):
        frame=r.frame(n/FPS)
        frames.append(frame.quantize(palette=palette,dither=Image.Dither.NONE))
    target=HERE/'voice-to-result.gif'
    frames[0].save(target,save_all=True,append_images=frames[1:],duration=1000//FPS,
                   loop=0,optimize=True,disposal=1)
    samples[-1].save(HERE/'voice-to-result-poster.png')
    # Small contact sheet for visual review; kept outside versioned artifacts.
    sheet.resize((660,round(sheet.height*660/W)),Image.Resampling.LANCZOS).save('/tmp/autonomous-voice-flow-review.png')
    print(f'{target}: {target.stat().st_size:,} bytes, {len(frames)} frames, {DURATION}s')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--font-dir',type=Path)
    render(parser.parse_args().font_dir)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
4B1 patch v3 - hook @0x236f6 (bl 0x230e4), BEFORE the mailbox is split by type.
Fires for EVERY received frame (type 1/2/3). The cave calls 0x230e4 (to get mb),
reads the FlexCAN mailbox, filters CAN-ID==0x4B1 (or --canid), reassembles ISO-TP ->
media store, and returns mb (transparent to FUN_0x236cc).

Usage: python apply_patch_v3.py <in.bin> <out.bin> [--canid 0xNNN]

--canid retargets the filtered CAN ID (default 0x4B1). EXPERIMENTAL - some head units
broadcast Bluetooth metadata on a different ID; see docs/TESTING.md. It only works for IDs
the cluster already accepts into a mailbox; other IDs need a mailbox reconfiguration.
"""
import struct, sys
BASE=0x5000; CAVE=0x83240
HOOK=0x236f6; ORIG=0x230e4                 # bl 0x230e4 -> bl CAVE; cave calls 0x230e4, returns mb
PERBUS=0x7a0d4; DLIST=0x8eff0; SABC=0x40009abc
BUF=0x40009300; SLEN=0x40009318; SIDX=0x40009319; SBUS=0x40009320  # +0 bus, +1 mb

# CAN ID the cave filters on. Default 0x4B1 (Bluetooth on the car this was built on).
# --canid retargets it - EXPERIMENTAL. It only works for IDs the cluster already receives into a
# mailbox (its acceptance table @0x79446 holds: 4B1, 4B3, 4C0, 4C1, 4C6, 4C7, 4D0, 4D2, 4D4, 4D5).
# Our hook sits in the mailbox dispatch BEFORE any ID routing, so any accepted ID reaches the cave;
# an ID outside that list is hardware-filtered before it ever arrives and would also need a mailbox
# reconfiguration (not done here). Verified in the emulator for 0x4C6; confirm on your own car.
CANID=0x4B1
_argv=sys.argv[1:]
if '--canid' in _argv:
    _i=_argv.index('--canid'); CANID=int(_argv[_i+1],0); del _argv[_i:_i+2]
assert 0<=CANID<=0x7FF, "CAN ID out of 11-bit range"

def hw(v): return struct.pack('>H', v&0xFFFF)
def ebl(src,dst):
    o=(dst-(src+4))&0x1FFFFFF; S=(o>>24)&1;i1=(o>>23)&1;i2=(o>>22)&1
    im10=(o>>12)&0x3FF;im11=(o>>1)&0x7FF;j1=(~(i1^S))&1;j2=(~(i2^S))&1
    return struct.pack('>HH',0xF000|(S<<10)|im10,0xD000|(j1<<13)|(j2<<11)|im11)

P=[]
def I(*a): P.append(a)
def L(n): P.append(('L',n))

I('push',[4,5,6,7])
I('ldrl',3,('c',SBUS)); I('strb_i',0,3,0)           # save bus
I('bl',ORIG)                                          # r0 = mb
I('ldrl',3,('c',SBUS)); I('strb_i',0,3,1)           # save mb
# mailbox = 0x7a0d4[bus] + mb*16 + 0x80
I('ldrb_i',0,3,0); I('ldrl',2,('c',PERBUS)); I('lsls_i',1,0,2); I('ldr_r',4,2,1)
I('ldrb_i',1,3,1); I('lsls_i',1,1,4); I('adds_r',4,4,1); I('adds_i8',4,0x80)
# CAN-ID == 0x4B1 ? (4B1 only - 4B0 carries the same text; handling both = interleave + buffer corruption)
I('ldr_iw',0,4,1); I('lsrs_i',0,0,18)
I('ldrl',1,('c',0x7FF)); I('ands',0,1)
I('ldrl',1,('c',CANID)); I('cmp_r',0,1); I('bcc',1,'L_orig')
I('adds_i3',6,4,0); I('adds_i8',6,8)                 # r6 = data (mb+8)
I('ldrb_i',0,6,0); I('lsrs_i',2,0,4)
I('cmp_i',2,1); I('bcc',0,'L_ff')
I('cmp_i',2,2); I('bcc',0,'L_cf')
I('cmp_i',2,0); I('bcc',0,'L_sf')      # Single Frame (short/empty field)
I('b','L_orig')
L('L_sf')                               # SF: whole payload in one frame -> immediately complete
I('ldrb_i',1,6,0); I('movs_i',0,0x0F); I('ands',1,0)   # L = d0 & 0x0F
I('ldrl',3,('c',SLEN)); I('strb_i',1,3,0)              # exp_len = L
I('ldrl',5,('c',BUF)); I('movs_i',2,0)                 # i=0
L('L_sfl')
I('cmp_r',2,1); I('bcc',2,'L_sfd')                     # i >= L -> done
I('adds_r',0,6,2); I('ldrb_i',0,0,1); I('strb_r',0,5,2)  # BUF[i] = d[1+i]
I('adds_i3',2,2,1); I('b','L_sfl')
L('L_sfd')
I('ldrl',3,('c',SIDX)); I('strb_i',1,3,0); I('b','L_check')  # idx = L (complete)
L('L_ff')
# Clamp 23: text <=19 chars. This is the architecture's natural ceiling (the stock field
# handler caps at 19, the title store is 20B, the renderer buffers are 20B). It also matches
# the Bluetooth module's own limit: it truncates every field to 18 chars + a '~' marker (19
# total) at the source, so nothing longer ever arrives. Pairs with apply_render.py (title ->
# sp+0x40 20B, artist -> sp+0x54). BUF is 24B (0x40009300..17), exp_len=23 -> null@BUF[23]
# = last byte of BUF (no collision with SLEN). See docs/HOW_IT_WORKS.md.
I('ldrb_i',1,6,1); I('cmp_i',1,23); I('bcc',9,'L_ffok'); I('movs_i',1,23)
L('L_ffok')
I('ldrl',3,('c',SLEN)); I('strb_i',1,3,0); I('ldrl',5,('c',BUF)); I('movs_i',2,0)
L('L_ffl')
I('adds_r',0,6,2); I('ldrb_i',0,0,2); I('strb_r',0,5,2)
I('adds_i3',2,2,1); I('cmp_i',2,6); I('bcc',3,'L_ffl')
I('ldrl',3,('c',SIDX)); I('movs_i',0,6); I('strb_i',0,3,0); I('b','L_check')
L('L_cf')
I('ldrl',3,('c',SIDX)); I('ldrb_i',2,3,0); I('cmp_i',2,0); I('bcc',0,'L_orig')
I('ldrl',5,('c',BUF)); I('movs_i',1,0)
L('L_cfl')
I('adds_r',0,2,1); I('cmp_i',0,23); I('bcc',2,'L_cfd')
I('adds_r',0,6,1); I('ldrb_i',0,0,1); I('adds_r',3,5,2); I('strb_r',0,3,1)
I('adds_i3',1,1,1); I('cmp_i',1,7); I('bcc',3,'L_cfl')
L('L_cfd')
I('adds_r',2,2,1); I('ldrl',3,('c',SIDX)); I('strb_i',2,3,0)
L('L_check')
I('ldrl',3,('c',SLEN)); I('ldrb_i',0,3,0); I('ldrl',3,('c',SIDX)); I('ldrb_i',2,3,0)
I('cmp_i',0,5); I('bcc',3,'L_orig'); I('cmp_r',2,0); I('bcc',3,'L_orig')
I('ldrl',5,('c',BUF)); I('movs_i',1,0); I('strb_r',1,5,0)
I('ldrb_i',1,5,1); I('cmp_i',1,0x12); I('bcc',1,'L_rst')
I('adds_i3',1,5,0); I('ldrl',2,('c',DLIST)); I('ldrl',4,('c',SABC))
L('L_dl')
I('ldr_iw',3,2,1); I('cmp_i',3,0); I('bcc',0,'L_rst')
I('ldrb_i',5,2,1); I('ldrb_i',0,1,0); I('cmp_r',5,0); I('bcc',1,'L_dn')
I('ldrb_i',5,2,0); I('cmp_i',5,0); I('bcc',1,'L_dn')
I('str_iw',2,4,0); I('ldrl',0,('lbit','L_ret')); I('mov_hi',14,0); I('bx',3)
L('L_ret'); I('b','L_rst')
L('L_dn'); I('adds_i8',2,8); I('b','L_dl')
L('L_rst')
I('ldrl',3,('c',SIDX)); I('movs_i',0,0); I('strb_i',0,3,0)
L('L_orig')
I('ldrl',3,('c',SBUS)); I('ldrb_i',0,3,1)            # r0 = mb (return value for FUN_0x236cc)
I('pop',[4,5,6,7])

def enc(m,a,addr,lab,lit):
    if m=='push': return hw(0xB400|0x0100|sum(1<<r for r in a[0]))
    if m=='pop':  return hw(0xBC00|0x0100|sum(1<<r for r in a[0]))
    if m=='movs_i': return hw(0x2000|(a[0]<<8)|(a[1]&0xFF))
    if m=='cmp_i':  return hw(0x2800|(a[0]<<8)|(a[1]&0xFF))
    if m=='adds_i8':return hw(0x3000|(a[0]<<8)|(a[1]&0xFF))
    if m=='adds_i3':return hw(0x1C00|(a[2]<<6)|(a[1]<<3)|a[0])
    if m=='adds_r': return hw(0x1800|(a[2]<<6)|(a[1]<<3)|a[0])
    if m=='lsls_i': return hw(0x0000|(a[2]<<6)|(a[1]<<3)|a[0])
    if m=='lsrs_i': return hw(0x0800|((a[2]&0x1F)<<6)|(a[1]<<3)|a[0])
    if m=='ands':   return hw(0x4000|(a[1]<<3)|a[0])
    if m=='cmp_r':  return hw(0x4280|(a[1]<<3)|a[0])
    if m=='mov_hi': return hw(0x4600|((a[0]>>3)<<7)|((a[1]&0xF)<<3)|(a[0]&7))
    if m=='ldrb_i': return hw(0x7800|(a[2]<<6)|(a[1]<<3)|a[0])
    if m=='strb_i': return hw(0x7000|(a[2]<<6)|(a[1]<<3)|a[0])
    if m=='strb_r': return hw(0x5400|(a[2]<<6)|(a[1]<<3)|a[0])
    if m=='ldr_r':  return hw(0x5800|(a[2]<<6)|(a[1]<<3)|a[0])
    if m=='ldr_iw': return hw(0x6800|(a[2]<<6)|(a[1]<<3)|a[0])
    if m=='str_iw': return hw(0x6000|(a[2]<<6)|(a[1]<<3)|a[0])
    if m=='bx':     return hw(0x4700|(a[0]<<3))
    if m=='ldrl':
        la=lit[a[1]]; off=la-((addr+4)&~3)
        assert 0<=off<=0x3FC and off%4==0, f"ldrl {off:#x}@{addr:#x}"
        return hw(0x4800|(a[0]<<8)|(off>>2))
    if m=='b':
        off=lab[a[0]]-(addr+4); assert -2048<=off<=2046
        return hw(0xE000|((off>>1)&0x7FF))
    if m=='bcc':
        off=lab[a[1]]-(addr+4); assert -256<=off<=254, f"bcc {off}@{addr:#x}"
        return hw(0xD000|(a[0]<<8)|((off>>1)&0xFF))
    if m=='bl': return ebl(addr,a[0])
    raise ValueError(m)

addr=CAVE; lab={}
for op in P:
    if op[0]=='L': lab[op[1]]=addr
    else: addr += 4 if op[0]=='bl' else 2
pool=addr+(addr&2); lits=[]
for op in P:
    if op[0]=='ldrl' and op[2] not in lits: lits.append(op[2])
lit={}; a=pool
for k in lits: lit[k]=a; a+=4
total=a-CAVE
def lv(k): return k[1] if k[0]=='c' else (lab[k[1]]|1)
out=bytearray(); addr=CAVE
for op in P:
    if op[0]=='L': continue
    out+=enc(op[0],op[1:],addr,lab,lit); addr += 4 if op[0]=='bl' else 2
while addr<pool: out+=hw(0x46C0); addr+=2
for k in lits: out+=struct.pack('>I',lv(k)); addr+=4
assert len(out)==total

def main():
    fi=_argv[0] if len(_argv)>0 else 'main.bin'
    fo=_argv[1] if len(_argv)>1 else 'main_patched.bin'
    d=bytearray(open(fi,'rb').read()); ho=HOOK-BASE; co=CAVE-BASE
    orig=bytes(d[ho:ho+4]); exp=ebl(HOOK,ORIG)
    print(f"CAN ID filter: {CANID:#05x}" + ("" if CANID==0x4B1 else "  (EXPERIMENTAL - non-default ID)"))
    print(f"HOOK @{HOOK:#x}: {orig.hex(' ')} (expected bl 0x230e4={exp.hex(' ')}) {'OK' if orig==exp else 'MISMATCH'}")
    assert orig==exp, "hook mismatch - unsupported firmware version"
    assert not any(d[co:co+total]), "cave not empty"
    print(f"CAVE {total} B; bl->cave {ebl(HOOK,CAVE).hex(' ')}")
    d[ho:ho+4]=ebl(HOOK,CAVE); d[co:co+total]=out
    open(fo,'wb').write(d); print("Saved",fo)

if __name__=='__main__': main()

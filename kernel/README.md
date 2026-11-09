# Custom kernel (PREEMPT, HZ=1000) for the sMS-200Ultra

`userpatches/` here is the versioned copy of the Armbian build inputs. Before building, copy it into the
Armbian clone (not tracked, ~5 GB):

    git clone --depth 1 https://github.com/armbian/build kernel/armbian-build
    cp -r kernel/userpatches/. kernel/armbian-build/userpatches/
    cd kernel/armbian-build
    ./compile.sh kernel BOARD=cubietruck BRANCH=current KERNEL_CONFIGURE=no KERNEL_GIT=shallow \
        KERNEL_BTF=no ENABLE_EXTENSIONS=sotm-audio

`extensions/sotm-audio.sh` runs after Armbian's own config hooks, which would otherwise force FTRACE,
KPROBES and RT_GROUP_SCHED back on. RT_GROUP_SCHED and AUDIT still end up enabled and are switched off
with the boot arguments `rt_group_sched=0 audit=0`. Built packages land in `armbian-build/output/debs/`.

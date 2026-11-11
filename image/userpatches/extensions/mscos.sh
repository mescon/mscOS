# mscOS image: a low-latency kernel without tracing overhead, on every board.
# custom_kernel_config runs after Armbian's armbian_kernel_config__* hooks (which force
# RT_GROUP_SCHED for containers and FTRACE/KPROBES for eBPF tooling) and can undo them.
# RT_GROUP_SCHED and AUDIT may still end up enabled (selected by other options); they are
# switched off at boot with rt_group_sched=0 audit=0 (mscos-setup).
function custom_kernel_config__mscos_audio() {
	opts_y+=("PREEMPT" "HZ_1000")
	opts_n+=("PREEMPT_NONE" "PREEMPT_VOLUNTARY" "HZ_250" "DEBUG_PREEMPT"
		"RT_GROUP_SCHED" "PSI" "SCHED_AUTOGROUP" "INIT_ON_ALLOC_DEFAULT_ON"
		"FTRACE" "FUNCTION_TRACER" "FUNCTION_GRAPH_TRACER" "DYNAMIC_FTRACE" "FTRACE_SYSCALLS"
		"KPROBES" "KPROBE_EVENTS" "BPF_KPROBE_OVERRIDE" "BPF_EVENTS")
}

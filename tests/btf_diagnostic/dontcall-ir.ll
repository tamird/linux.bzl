; RUN: not %clang_cc1 -triple x86_64-unknown-linux-gnu -x ir -emit-obj -O2 -fdiagnostics-show-inlining-chain %s -o /dev/null 2>&1 | FileCheck %s
; CHECK: error: call to 'fail' declared with 'error' attribute: dontcall IR error
; CHECK: note: called by function 'inner'
; CHECK: note: inlined by function 'outer'
; CHECK-NOT: PLEASE submit a bug report
; CHECK-NOT: Stack dump:

target triple = "x86_64-unknown-linux-gnu"
declare void @fail() "dontcall-error"="dontcall IR error"
define void @outer() !dbg !4 {
  call void @fail(), !dbg !7, !srcloc !9
  ret void, !dbg !8
}
!llvm.dbg.cu = !{!0}
!llvm.module.flags = !{!10}
!0 = distinct !DICompileUnit(language: DW_LANG_C99, file: !1, producer: "test", isOptimized: true, runtimeVersion: 0, emissionKind: FullDebug)
!1 = !DIFile(filename: "dontcall-ir.c", directory: "/")
!2 = !DISubroutineType(types: !3)
!3 = !{null}
!4 = distinct !DISubprogram(name: "outer", scope: !1, file: !1, line: 4, type: !2, scopeLine: 4, spFlags: DISPFlagDefinition | DISPFlagOptimized, unit: !0)
!5 = distinct !DISubprogram(name: "inner", scope: !1, file: !1, line: 2, type: !2, scopeLine: 2, spFlags: DISPFlagDefinition | DISPFlagOptimized, unit: !0)
!7 = !DILocation(line: 2, column: 1, scope: !5, inlinedAt: !8)
!8 = !DILocation(line: 4, column: 1, scope: !4)
!9 = !{i64 1}
!10 = !{i32 2, !"Debug Info Version", i32 3}

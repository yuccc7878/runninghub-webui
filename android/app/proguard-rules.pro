# Chaquopy / Python 相关 keep 规则
-keep class com.chaquo.python.** { *; }
-keep class com.yuccc.runninghub.** { *; }

# Python 模块运行时反射调用，不混淆
-keepclassmembers class * {
    @com.chaquo.python.* <methods>;
}
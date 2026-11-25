# The app's own R8 rules for the release build. Capacitor's consumer rules
# (node_modules/@capacitor/android/capacitor/proguard-rules.pro) keep the
# plugin class and its @PluginMethod methods, which Capacitor calls by
# reflection; Media3 and AndroidX bring their own rules.

# Keep file names and line numbers so a crash report from a release build can
# be read, while class names stay shortened.
-keepattributes SourceFile,LineNumberTable
-renamesourcefileattribute SourceFile

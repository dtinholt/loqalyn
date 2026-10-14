/* Minimal relocatable launcher. All Python imports come from the app bundle. */
#include <mach-o/dyld.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv) {
    char raw[PATH_MAX], executable[PATH_MAX], python[PATH_MAX], script[PATH_MAX];
    uint32_t size = sizeof raw;
    if (_NSGetExecutablePath(raw, &size) || !realpath(raw, executable)) return 70;
    char *last = strrchr(executable, '/');
    if (!last) return 70;
    *last = 0;
    if (snprintf(python, sizeof python, "%s/SwitchyardPython", executable) >= sizeof python ||
        snprintf(script, sizeof script, "%s/../Resources/bootstrap.py", executable) >= sizeof script) return 70;
    char **args = calloc((size_t)argc + 5, sizeof(char *));
    if (!args) return 70;
    args[0] = python; args[1] = "-I"; args[2] = "-B"; args[3] = script;
    for (int i = 1; i < argc; i++) args[i + 3] = argv[i];
    /* -I isolates imports; -B keeps every byte of the signed bundle immutable. */
    execv(python, args);
    perror("Switchyard could not start its bundled runtime");
    free(args);
    return 70;
}

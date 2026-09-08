* PAYREC - record layout plus the shop's linkage macro.
* Without this member PAYCALC has no PAYPOST dependency at all.
         MACRO
         SITELINK &PGM=,&LIB=
         L     15,=V(&PGM)
         BALR  14,15
         MEND
*
PAYRECD  DSECT
PAYEMPNO DS    CL6
PAYNAME  DS    CL30
PAYGROSS DS    PL4
PAYRECL  EQU   *-PAYRECD
